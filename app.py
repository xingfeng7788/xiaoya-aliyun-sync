import os
import io
import json
import time
import base64
import traceback
import uuid
import _thread

import requests
import qrcode
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from dotenv import load_dotenv
from urllib.parse import unquote
from bs4 import BeautifulSoup
from aligo import Aligo
from aligo.core.Config import (
    AUTH_HOST, PASSPORT_HOST, API_HOST,
    V2_OAUTH_AUTHORIZE, NEWLOGIN_QRCODE_GENERATE_DO,
    NEWLOGIN_QRCODE_QUERY_DO, V2_ACCOUNT_TOKEN,
    CLIENT_ID, UNI_PARAMS, UNI_HEADERS
)

from db import (
    init_db, get_config, set_config, delete_config,
    get_all_configs, save_ali_token, get_ali_token, sync_env_to_db
)

# 加载 .env 文件
load_dotenv()

app = Flask(__name__)

# 初始化数据库并同步环境变量
init_db()
sync_env_to_db()

# --- 基础配置 (优先从数据库读取，否则从环境变量) ---
def cfg(key, default=""):
    return get_config(key) or os.getenv(key, default)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 5000))

# 全局初始化 Aligo 实例
_ali_instance = None

# QR 码登录状态
_qr_login_state = {
    'active': False,
    'qr_image_b64': None,
    'status': 'idle',  # idle, waiting, scanned, confirmed, expired, error
    'message': '',
}


def _get_refresh_token():
    """优先从数据库获取 refresh_token，其次从环境变量"""
    token_info = get_ali_token()
    if token_info and token_info.get('refresh_token'):
        return token_info['refresh_token']
    return cfg('ALI_REFRESH_TOKEN', '')


def get_ali(force_new=False):
    global _ali_instance
    if _ali_instance is None or force_new:
        refresh_token = _get_refresh_token()
        if not refresh_token:
            raise Exception("未配置阿里云盘 Token，请先通过页面扫码登录或在配置管理中设置 ALI_REFRESH_TOKEN")
        _ali_instance = Aligo(refresh_token=refresh_token, re_login=False, use_aria2=True)
        # 登录成功后，保存 token 到数据库
        if _ali_instance.token:
            save_ali_token(
                refresh_token=_ali_instance.token.refresh_token,
                access_token=_ali_instance.token.access_token,
                token_data=_ali_instance.token.to_dict() if hasattr(_ali_instance.token, 'to_dict') else None
            )
    return _ali_instance


def _start_qr_login():
    """启动二维码登录流程（后台线程调用）"""
    global _qr_login_state, _ali_instance
    try:
        _qr_login_state['active'] = True
        _qr_login_state['status'] = 'waiting'
        _qr_login_state['message'] = '请使用阿里云盘 APP 扫描二维码'

        session = requests.session()
        session.trust_env = False
        session.headers.update(UNI_HEADERS)

        # 1. 获取 session
        session.get(AUTH_HOST + V2_OAUTH_AUTHORIZE, params={
            'login_type': 'custom',
            'response_type': 'code',
            'redirect_uri': 'https://www.aliyundrive.com/sign/callback',
            'client_id': CLIENT_ID,
            'state': r'{"origin":"file://"}',
        }, stream=True, timeout=30).close()

        # 2. 生成二维码
        response = session.get(
            PASSPORT_HOST + NEWLOGIN_QRCODE_GENERATE_DO, params=UNI_PARAMS,
            timeout=30
        )
        data = response.json()['content']['data']
        qr_link = data['codeContent']

        # 3. 生成二维码图片 base64
        qr_img = qrcode.make(qr_link)
        buf = io.BytesIO()
        qr_img.save(buf, format='PNG')
        _qr_login_state['qr_image_b64'] = base64.b64encode(buf.getvalue()).decode('utf-8')

        # 4. 轮询扫码状态
        timeout_at = time.time() + 120  # 2分钟超时
        while time.time() < timeout_at:
            response = session.post(
                PASSPORT_HOST + NEWLOGIN_QRCODE_QUERY_DO,
                data=data, params=UNI_PARAMS, timeout=30
            )
            login_data = response.json()['content']['data']
            qr_status = login_data['qrCodeStatus']

            if qr_status == 'NEW':
                pass
            elif qr_status == 'SCANED':
                _qr_login_state['status'] = 'scanned'
                _qr_login_state['message'] = '已扫描，等待确认...'
            elif qr_status == 'CONFIRMED':
                _qr_login_state['status'] = 'confirmed'
                _qr_login_state['message'] = '登录成功，正在初始化...'

                # 解析 token
                biz_ext = response.json()['content']['data']['bizExt']
                biz_ext = base64.b64decode(biz_ext).decode('gb18030')
                refresh_token = json.loads(biz_ext)['pds_login_result']['refreshToken']

                # 用 refresh_token 换取完整 token
                token_resp = session.post(
                    API_HOST + V2_ACCOUNT_TOKEN,
                    json={'refresh_token': refresh_token, 'grant_type': 'refresh_token'},
                    timeout=30
                )
                if token_resp.status_code == 200:
                    token_data = token_resp.json()
                    save_ali_token(
                        refresh_token=token_data.get('refresh_token', refresh_token),
                        access_token=token_data.get('access_token', ''),
                        token_data=token_data
                    )
                    # 重新初始化 Aligo 实例
                    _ali_instance = None
                    _qr_login_state['status'] = 'confirmed'
                    _qr_login_state['message'] = '登录成功！'
                else:
                    save_ali_token(refresh_token=refresh_token)
                    _ali_instance = None
                    _qr_login_state['status'] = 'confirmed'
                    _qr_login_state['message'] = '登录成功！'
                return
            else:
                _qr_login_state['status'] = 'expired'
                _qr_login_state['message'] = '二维码已过期，请重新获取'
                return
            time.sleep(3)

        _qr_login_state['status'] = 'expired'
        _qr_login_state['message'] = '二维码已超时，请重新获取'
    except Exception as e:
        traceback.print_exc()
        _qr_login_state['status'] = 'error'
        _qr_login_state['message'] = f'登录出错: {str(e)}'
    finally:
        _qr_login_state['active'] = False


def _is_auth_error(e):
    """判断是否为认证相关错误"""
    msg = str(e).lower()
    return any(kw in msg for kw in ['refreshfailed', 'token', 'unauthorized', '401', 'invalidtoken', 'expired'])


@app.route('/api/aliyun/files', methods=['POST'])
def aliyun_files():
    """获取阿里云盘文件列表"""
    parent_file_id = request.json.get('parent_file_id', 'root')
    drive_id = request.json.get('drive_id')
    
    try:
        ali = get_ali()
        # 如果未提供 drive_id，使用默认 drive_id
        if not drive_id:
            drive_id = ali.default_drive_id

        # 获取文件列表
        files = ali.get_file_list(parent_file_id=parent_file_id, drive_id=drive_id)
        
        # 序列化结果
        file_list = []
        for f in files:
            file_list.append({
                'file_id': f.file_id,
                'name': f.name,
                'type': f.type,  # 'file' or 'folder'
                'size': f.size,
                'updated_at': str(f.updated_at),
                'drive_id': f.drive_id
            })
            
        # 获取当前文件夹信息（用于面包屑等，如果不是 root）
        current_folder = None
        if parent_file_id != 'root':
            f = ali.get_file(file_id=parent_file_id)
            if f:
                current_folder = {'file_id': f.file_id, 'name': f.name, 'parent_file_id': f.parent_file_id}
        
        return jsonify({
            "status": "success", 
            "files": file_list, 
            "current_folder": current_folder,
            "drive_id": drive_id
        })
    except Exception as e:
        traceback.print_exc()
        error_resp = {"status": "error", "message": str(e)}
        if _is_auth_error(e):
            error_resp["need_login"] = True
        return jsonify(error_resp)


import threading

def background_download(ali, file_ids, drive_id):
    """后台下载任务"""
    try:
        download_path = cfg('ALI_DOWNLOAD_PATH', '/downloads')
        print(f"Starting download for {len(file_ids)} files to {download_path}")
        if not os.path.exists(download_path):
            os.makedirs(download_path)

        # 1. 获取文件对象 (Download 需要 BaseFile 对象或类似结构)
        # aligo.download_files 需要 BaseFile 对象列表
        # 我们可以先批量获取文件信息
        # 这里的 batch_get_files 返回的是 BatchSubResponse 列表
        batch_responses = ali.batch_get_files(file_ids, drive_id=drive_id)
        
        target_files = []
        for resp in batch_responses:
            if resp.body and hasattr(resp.body, 'file_id'):
                 target_files.append(resp.body)
        
        if not target_files:
            print("No valid files found to download.")
            return

        # 2. 执行下载
        # 区分文件和文件夹
        # download_files 只能下载文件，download_folder 下载文件夹
        # 我们遍历处理
        
        for f in target_files:
            try:
                if f.type == 'file':
                    ali.download_file(file=f, local_folder=download_path)
                elif f.type == 'folder':
                    ali.download_folder(f.file_id, local_folder=download_path)
            except Exception as e:
                print(f"Download failed for {f.name}: {e}")

        print("Download task completed.")
    except Exception as e:
        print(f"Background download error: {e}")


@app.route('/api/aliyun/download', methods=['POST'])
def aliyun_download():
    """下载选中文件到宿主机映射目录"""
    file_ids = request.json.get('file_ids', [])
    drive_id = request.json.get('drive_id')

    if not file_ids:
        return jsonify({"status": "error", "message": "未选择任何文件"})

    try:
        ali = get_ali()
        # 启动后台线程下载
        thread = threading.Thread(target=background_download, args=(ali, file_ids, drive_id))
        thread.start()
        
        return jsonify({
            "status": "success", 
            "message": f"已开始下载 {len(file_ids)} 个任务到服务器 {cfg('ALI_DOWNLOAD_PATH', '/downloads')} 目录"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/api/aliyun/proxy', methods=['GET'])
def aliyun_proxy():
    """代理下载阿里云盘文件 (解决 Referer 问题)"""
    file_id = request.args.get('file_id')
    drive_id = request.args.get('drive_id')
    file_name = request.args.get('file_name', 'downloaded_file')

    if not file_id:
        return "Missing file_id", 400

    try:
        ali = get_ali()
        # 1. 获取下载链接
        download_info = ali.get_download_url(file_id=file_id, drive_id=drive_id)
        
        if not download_info or not download_info.url:
            return "Failed to get download URL", 500

        # 2. 请求文件内容 (带 Referer)
        headers = {
            'Referer': 'https://www.aliyundrive.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'
        }
        
        # 3. 流式传输
        req = requests.get(download_info.url, headers=headers, stream=True)
        
        if req.status_code != 200:
            return f"Upstream error: {req.status_code}", req.status_code

        def generate():
            for chunk in req.iter_content(chunk_size=1024 * 1024): # 1MB chunks
                yield chunk

        # 4. 构建响应，透传 Headers
        response = Response(stream_with_context(generate()), status=200)
        response.headers['Content-Type'] = req.headers.get('Content-Type', 'application/octet-stream')
        response.headers['Content-Length'] = req.headers.get('Content-Length')
        
        # 处理文件名编码 (简单处理，通常浏览器能自动识别 UTF-8)
        from urllib.parse import quote
        encoded_filename = quote(file_name)
        response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
        
        return response

    except Exception as e:
        traceback.print_exc()
        return f"Proxy error: {str(e)}", 500


@app.route('/api/aliyun/delete', methods=['POST'])
def aliyun_delete():
    """批量移动文件到回收站"""
    file_ids = request.json.get('file_ids', [])
    drive_id = request.json.get('drive_id')

    if not file_ids:
        return jsonify({"status": "error", "message": "未选择文件"})

    try:
        ali = get_ali()
        ali.batch_move_to_trash(file_ids, drive_id=drive_id)
        return jsonify({"status": "success", "message": "已移入回收站"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/api/aliyun/mkdir', methods=['POST'])
def aliyun_mkdir():
    """创建文件夹"""
    name = request.json.get('name')
    parent_file_id = request.json.get('parent_file_id', 'root')
    drive_id = request.json.get('drive_id')

    if not name:
        return jsonify({"status": "error", "message": "名称不能为空"})

    try:
        ali = get_ali()
        ali.create_folder(name, parent_file_id=parent_file_id, drive_id=drive_id)
        return jsonify({"status": "success", "message": "创建成功"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


def background_upload(ali, local_path, parent_file_id, drive_id):
    """后台上传任务"""
    try:
        print(f"Starting background upload: {local_path}")
        # upload_file 会自动处理分片和秒传
        ali.upload_file(local_path, parent_file_id=parent_file_id, drive_id=drive_id)
        print(f"Upload finished: {local_path}")
    except Exception as e:
        print(f"Upload failed: {e}")
    finally:
        # 上传完成后（无论成功失败）删除临时文件
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except:
                pass


@app.route('/api/aliyun/upload', methods=['POST'])
def aliyun_upload():
    """上传文件 (先存临时目录，再后台上传)"""
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "未上传文件"})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "文件名为空"})
        
    parent_file_id = request.form.get('parent_file_id', 'root')
    drive_id = request.form.get('drive_id')
    
    # 确保临时目录存在
    temp_dir = os.path.join(os.getcwd(), 'temp_uploads')
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)
    
    local_path = os.path.join(temp_dir, file.filename)
    
    try:
        file.save(local_path)
        
        ali = get_ali()
        # 启动后台线程上传
        thread = threading.Thread(target=background_upload, args=(ali, local_path, parent_file_id, drive_id))
        thread.start()
        
        return jsonify({"status": "success", "message": "文件已接收，正在后台上传到阿里云盘..."})
    except Exception as e:
        # 如果保存失败，尝试清理
        if os.path.exists(local_path):
            os.remove(local_path)
        return jsonify({"status": "error", "message": str(e)})


def get_alist_storages():
    """获取 Alist 所有存储挂载信息"""
    try:
        res = requests.get(f"{cfg('ALIST_URL', 'http://localhost:5234')}/api/admin/storage/list?page=1&per_page=0",
                           headers={'Authorization': cfg('ADMIN_TOKEN', '')})
        data = res.json()
        if data.get('code') == 200:
            return data.get('data', {}).get('content', [])
    except Exception as e:
        print(f"Get storage list failed: {e}")
    return []


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/search', methods=['POST'])
def search():
    keyword = request.json.get('keyword', '')
    if not keyword:
        return jsonify({"status": "error", "message": "请输入关键词"})

    search_url = f"{cfg('XIAOYA_URL', 'http://localhost:5678')}/search"
    params = {"box": keyword, "url": "", "type": "video"}
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}

    try:
        resp = requests.get(search_url, params=params, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        results = []
        seen_paths = set()

        for link in soup.find_all('a'):
            href = link.get('href')
            if not href:
                continue

            # 过滤非资源链接（首页、外部链接等）
            if href == '/' or href.startswith(('http://', 'https://', 'javascript:')):
                continue

            # 解码路径并规范化
            candidate = unquote(href)
            if not candidate.startswith('/'):
                candidate = '/' + candidate

            if candidate not in seen_paths:
                if any(x in candidate for x in ['/@manage', '/@login']): continue
                seen_paths.add(candidate)
                results.append({
                    "name": candidate.split('/')[-1], 
                    "path": candidate,
                    "url": f"{cfg('XIAOYA_URL', 'http://localhost:5678').rstrip('/')}{candidate}"
                })

        return jsonify({"status": "success", "results": results[:100]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


def recursive_share_transfer(ali, share_token_obj, source_parent_id, target_parent_id):
    """
    递归转存分享文件
    :param ali: Aligo 实例
    :param share_token_obj: 分享 token 对象
    :param source_parent_id: 分享中的源文件夹 ID
    :param target_parent_id: 也就是保存到的目标文件夹 ID
    """
    try:
        # 获取源文件夹下的所有内容
        children = ali.get_share_file_list(share_token_obj, parent_file_id=source_parent_id)
        
        files = []
        folders = []
        
        for child in children:
            if child.type == 'file':
                files.append(child)
            else:
                folders.append(child)
                
        # 1. 批量转存文件
        if files:
            file_ids = [f.file_id for f in files]
            try:
                ali.batch_share_file_saveto_drive(file_ids, share_token_obj, target_parent_id)
            except Exception as e:
                print(f"Batch transfer files failed: {e}")
                
        # 2. 递归处理文件夹
        for folder in folders:
            try:
                # 在目标目录创建对应的新文件夹
                new_folder = ali.create_folder(folder.name, target_parent_id)
                if new_folder:
                    # 递归转存
                    recursive_share_transfer(ali, share_token_obj, folder.file_id, new_folder.file_id)
            except Exception as e:
                print(f"Process folder {folder.name} failed: {e}")
    except Exception as e:
        print(f"Recursive transfer failed for {source_parent_id}: {e}")


def find_matched_storage(target_path, storage_data):
    """
    根据目标路径匹配 AList 的挂载存储 (最长前缀匹配)
    """
    if not target_path:
        return None

    # 统一规范化 target_path: 确保以 / 开头，去除首尾空白和尾部 /
    # 例如: " /abc/def/ " -> "/abc/def", "/" -> "/"
    target_path = "/" + target_path.strip().strip("/")
    
    best_match = None
    max_len = -1

    for item in storage_data:
        mount_path = item.get('mount_path', '')
        # 统一规范化 mount_path
        mount_path = "/" + mount_path.strip().strip("/")
        
        # 匹配逻辑:
        # 1. 根目录 / 特殊处理: 总是匹配
        # 2. 精确匹配: target == mount
        # 3. 前缀匹配: target starts with mount + "/" (确保是目录级匹配，避免 /movie1 匹配 /movie)
        
        is_match = False
        
        if mount_path == "/":
            is_match = True
        elif target_path == mount_path:
            is_match = True
        elif target_path.startswith(mount_path + "/"):
            is_match = True
            
        if is_match:
            # 记录匹配长度最长的那个 (Mount Path 越长表示越具体的子目录挂载)
            if len(mount_path) > max_len:
                max_len = len(mount_path)
                best_match = item

    return best_match


@app.route('/api/transfer', methods=['POST'])
def transfer():
    full_path = request.json.get('path', '').split('#')[0]
    # 规范化路径
    if full_path.startswith('.'): full_path = full_path[1:]
    if not full_path.startswith('/'): full_path = '/' + full_path
    full_path = full_path.rstrip('/')

    # 1. 获取所有存储并匹配 (用于获取 share_id)
    all_storages = get_alist_storages()
    
    # 迭代解析路径，处理 Alias 重定向
    max_redirects = 5
    matched_storage = None
    mount_path = ""
    
    for _ in range(max_redirects):
        matched_storage = find_matched_storage(full_path, all_storages)

        if not matched_storage:
            return jsonify({"status": "error", "message": "未找到对应的存储挂载，请确认路径是否正确"})

        mount_path = matched_storage['mount_path']
        driver = matched_storage['driver']

        # 如果是 Alias，进行重定向解析
        if driver == 'Alias':
            try:
                addition = json.loads(matched_storage.get('addition', '{}'))
                paths_str = addition.get('paths', '')
                # Alias 的 paths 可能有多行，通常取第一个
                # 格式可能是 "目标存储名:/目标路径" 或直接 "/目标路径"
                target_raw = paths_str.split('\n')[0].strip()
                
                # 解析目标路径：尝试去除 "Name:" 前缀
                target_path_root = target_raw
                if ':' in target_raw:
                    # 分割 "Name:/Path" -> ["Name", "/Path"]
                    parts = target_raw.split(':', 1)
                    if len(parts) > 1 and parts[1].strip().startswith('/'):
                        target_path_root = parts[1].strip()
                
                # 拼接新路径: TargetRoot + (FullPath - MountPath)
                # 例如: Mount=/A, Full=/A/B, Target=/C -> New=/C/B
                suffix = ""
                if full_path == mount_path:
                    suffix = ""
                elif full_path.startswith(mount_path + "/"):
                    suffix = full_path[len(mount_path):]
                
                # 更新 full_path 继续循环匹配
                full_path = (target_path_root + suffix).replace('//', '/')
                print(f"Alias redirection: {mount_path} -> {target_path_root} | New path: {full_path}")
                continue
                
            except Exception as e:
                print(f"Alias resolution failed: {e}")
                return jsonify({"status": "error", "message": f"解析 Alias 挂载 '{mount_path}' 失败: {str(e)}"})

        # 校验驱动类型
        if driver != 'AliyundriveShare2Open':
             return jsonify({
                 "status": "error", 
                 "message": f"匹配到的存储挂载 '{mount_path}' 类型为 '{driver}'。目前仅支持 'AliyundriveShare2Open' 类型挂载的转存，不支持此类型。"
             })
        
        # 如果是支持的驱动，跳出循环继续后续逻辑
        break

    if not matched_storage: # Should be caught inside loop, but safety check
         return jsonify({"status": "error", "message": "路径解析失败"})

    try:
        addition = json.loads(matched_storage.get('addition', '{}'))
    except:
        return jsonify({"status": "error", "message": "无法解析存储配置信息"})

    share_id = addition.get('share_id')
    share_pwd = addition.get('share_pwd', '')
    
    if not share_id:
        return jsonify({"status": "error", "message": "仅支持阿里云盘分享转存 (未找到 share_id)"})

    try:
        ali = get_ali()
        share_token_obj = ali.get_share_token(share_id, share_pwd=share_pwd)
        
        # 获取挂载点的根文件夹 ID (默认为 root)
        mount_root_id = addition.get('root_folder_id') or 'root'
        
        # 计算相对路径: full_path - mount_path
        # 注意: mount_path 可能是 /A, full_path 可能是 /A/B -> rel_path = /B
        # 如果 full_path == mount_path, rel_path = ""
        
        if full_path == mount_path:
            rel_path = ""
        elif full_path.startswith(mount_path + "/"):
            rel_path = full_path[len(mount_path):]
        else:
             # 理论上不应该走到这里，因为前面已经 find_matched_storage 校验过
             # 但如果 Alias 重定向后 mount_path 变了，可能需要重新确认
             # 这里简单处理: 如果不匹配前缀，尝试直接用 full_path (容错)
             print(f"Warning: path mismatch after alias resolution. Mount: {mount_path}, Full: {full_path}")
             rel_path = full_path

        parts_to_traverse = [p for p in rel_path.strip('/').split('/') if p]

        # 3. 逐层下钻 (Drill down)
        current_file_id = mount_root_id
        found_target = None
        
        if not parts_to_traverse:
             # 如果没有路径需要遍历，说明目标就是挂载根目录
             # 获取根目录信息（为了拿到 name 和 type）
             try:
                 if current_file_id == 'root':
                     # Root 特殊处理，无法直接 get_file
                     found_target = type('obj', (object,), {'name': full_path.split('/')[-1] or "Root", 'file_id': 'root', 'type': 'folder'})
                 else:
                     # 获取指定 ID 的文件信息
                     # get_share_file_list 只能列出子文件，不能获取当前文件夹详情？
                     # aligo 的 get_file 需要 file_id，但在 share 模式下可能受限
                     # 这里变通一下：我们认为它是一个文件夹
                     found_target = type('obj', (object,), {'name': full_path.split('/')[-1] or "Target", 'file_id': current_file_id, 'type': 'folder'})
             except Exception as e:
                 print(f"Get root info failed: {e}")
                 found_target = type('obj', (object,), {'name': "Target", 'file_id': current_file_id, 'type': 'folder'})
        else:
            for i, part in enumerate(parts_to_traverse):
                # 列出当前层级的文件
                files = ali.get_share_file_list(share_token_obj, parent_file_id=current_file_id)
                found_in_level = None
                for f in files:
                    if f.name == part:
                        found_in_level = f
                        break
                
                if not found_in_level:
                    return jsonify({"status": "error", "message": f"在分享路径中未找到: {part} (上一级 ID: {current_file_id})"})
                
                current_file_id = found_in_level.file_id
                if i == len(parts_to_traverse) - 1:
                    found_target = found_in_level

        # 4. 执行转存
        if not found_target:
             return jsonify({"status": "error", "message": "无法定位目标文件"})

        target_name = found_target.name
        save_to_parent_id = cfg('ALI_TARGET_FOLDER_ID', 'root')
        
        # 如果是文件：创建同名文件夹（去后缀），转存该文件
        if getattr(found_target, 'type', 'folder') == 'file':
            target_name = os.path.splitext(target_name)[0]
            transfer_file_ids = [found_target.file_id]
            
            # 创建目标目录
            try:
                new_folder = ali.create_folder(target_name, cfg('ALI_TARGET_FOLDER_ID', 'root'))
                if new_folder:
                    save_to_parent_id = new_folder.file_id
                    ali.batch_share_file_saveto_drive(transfer_file_ids, share_token_obj, save_to_parent_id)
            except Exception as create_err:
                print(f"Create folder or transfer file failed: {create_err}")
                
        else:
            # 如果是文件夹（或 Root）：创建同名文件夹，递归转存
            try:
                new_folder = ali.create_folder(target_name, cfg('ALI_TARGET_FOLDER_ID', 'root'))
                if new_folder:
                    save_to_parent_id = new_folder.file_id
                    # 使用递归转存
                    recursive_share_transfer(ali, share_token_obj, found_target.file_id, save_to_parent_id)
                else:
                    return jsonify({"status": "error", "message": "创建目标文件夹失败"})
            except Exception as create_err:
                print(f"Create folder failed: {create_err}")
                return jsonify({"status": "error", "message": f"创建文件夹失败: {str(create_err)}"})

        return jsonify({
            "status": "success",
            "message": f"成功提交转存任务！资源 [{target_name}] 已开始转存到阿里云盘。"
        })

    except Exception as e:
        traceback.print_exc()
        print(f"Transfer error: {e}")
        return jsonify({"status": "error", "message": f"转存失败: {str(e)}"})


# ==================== 配置管理 API ====================

@app.route('/api/config/list', methods=['GET'])
def config_list():
    """获取所有配置"""
    try:
        configs = get_all_configs()
        return jsonify({"status": "success", "configs": configs})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/api/config/set', methods=['POST'])
def config_set():
    """设置配置"""
    key = request.json.get('key', '').strip()
    value = request.json.get('value', '').strip()
    description = request.json.get('description', '')

    if not key or not value:
        return jsonify({"status": "error", "message": "key 和 value 不能为空"})

    try:
        set_config(key, value, description)
        return jsonify({"status": "success", "message": f"配置 {key} 已保存"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/api/config/delete', methods=['POST'])
def config_delete():
    """删除配置"""
    key = request.json.get('key', '').strip()
    if not key:
        return jsonify({"status": "error", "message": "key 不能为空"})

    try:
        delete_config(key)
        return jsonify({"status": "success", "message": f"配置 {key} 已删除"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ==================== 阿里云盘扫码登录 API ====================

@app.route('/api/ali/qr_login', methods=['POST'])
def ali_qr_login():
    """发起二维码登录"""
    import threading
    if _qr_login_state['active']:
        return jsonify({"status": "error", "message": "已有登录流程进行中"})

    _qr_login_state['qr_image_b64'] = None
    _qr_login_state['status'] = 'idle'
    _qr_login_state['message'] = ''

    thread = threading.Thread(target=_start_qr_login, daemon=True)
    thread.start()

    # 等待二维码生成（最多10秒）
    for _ in range(100):
        if _qr_login_state['qr_image_b64'] or _qr_login_state['status'] in ('error', 'expired'):
            break
        time.sleep(0.1)

    return jsonify({
        "status": "success",
        "qr_image": _qr_login_state['qr_image_b64'],
        "login_status": _qr_login_state['status'],
        "message": _qr_login_state['message'],
    })


@app.route('/api/ali/qr_status', methods=['GET'])
def ali_qr_status():
    """查询二维码扫码状态"""
    return jsonify({
        "status": "success",
        "login_status": _qr_login_state['status'],
        "message": _qr_login_state['message'],
        "active": _qr_login_state['active'],
    })


@app.route('/api/ali/token_status', methods=['GET'])
def ali_token_status():
    """检查当前 token 状态"""
    token_info = get_ali_token()
    has_token = bool(token_info and token_info.get('refresh_token'))

    # 尝试验证 token 是否有效
    valid = False
    if has_token:
        try:
            ali = get_ali()
            ali.get_user()
            valid = True
        except Exception:
            valid = False

    return jsonify({
        "status": "success",
        "has_token": has_token,
        "valid": valid,
        "updated_at": token_info.get('updated_at', '') if token_info else '',
    })


if __name__ == '__main__':
    app.run(host=HOST, port=PORT)
