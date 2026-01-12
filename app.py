import os
import json
import traceback

import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from urllib.parse import unquote
from bs4 import BeautifulSoup
from aligo import Aligo

# 加载 .env 文件
load_dotenv()

app = Flask(__name__)

# --- 基础配置 ---
ALIST_URL = os.getenv("ALIST_URL", "http://localhost:5234")
XIAOYA_URL = os.getenv("XIAOYA_URL", "http://localhost:5678")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 5000))

# --- 阿里云盘配置 (aligo 模式) ---
# 请在 .env 中设置 ALI_REFRESH_TOKEN
ALI_REFRESH_TOKEN = os.getenv("ALI_REFRESH_TOKEN", "")
# 目标文件夹 ID，默认为根目录
ALI_TARGET_FOLDER_ID = os.getenv("ALI_TARGET_FOLDER_ID", "root")

# 全局初始化 Aligo 实例
# 注意：第一次启动时，如果没有 refresh_token，aligo 可能会在控制台打印二维码
_ali_instance = None


def get_ali():
    global _ali_instance
    if _ali_instance is None:
        if not ALI_REFRESH_TOKEN:
            print("警告: 未设置 ALI_REFRESH_TOKEN，Aligo 将进入扫码模式")
        _ali_instance = Aligo(refresh_token=ALI_REFRESH_TOKEN)
    return _ali_instance


def get_alist_storages():
    """获取 Alist 所有存储挂载信息"""
    try:
        res = requests.get(f"{ALIST_URL}/api/admin/storage/list?page=1&per_page=0",
                           headers={'Authorization': ADMIN_TOKEN})
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

    search_url = f"{XIAOYA_URL}/search"
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
                    "url": f"{XIAOYA_URL.rstrip('/')}{candidate}"
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
    # 格式化路径，确保以 / 开头，且末尾处理一致
    best_match = None
    max_len = -1

    for item in storage_data:
        mount_path, driver = item['mount_path'], item['driver']
        # 处理根目录匹配情况
        formatted_mount = "/" + mount_path.strip("/")
        if mount_path == "/":
            formatted_mount = "/"

        # 检查 target_path 是否以挂载点开头
        # 注意：为了防止 /movie1 匹配到 /movie，通常会在末尾加 / 判断或精确匹配
        check_path = target_path + "/"
        check_mount = formatted_mount if formatted_mount == "/" else formatted_mount + "/"

        if check_path.startswith(check_mount):
            # 记录匹配长度最长的那个
            if len(formatted_mount) > max_len:
                max_len = len(formatted_mount)
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
    # storages = get_alist_storages()
    storages = [ga for ga in get_alist_storages() if ga['driver'] == 'AliyundriveShare2Open']
    # for storage in storages:
    #     print(storage['mount_path'], storage['id'])
    matched_storage = find_matched_storage(full_path, storages)
    # mount_path = ""
    #
    # for s in storages:
    #     m_path = s.get('mount_path')
    #     if not m_path: continue
    #     if full_path == m_path or full_path.startswith(m_path + '/'):
    #         if len(m_path) > len(mount_path):
    #             mount_path = m_path
    #             matched_storage = s

    if not matched_storage:
        return jsonify({"status": "error", "message": "未找到对应的存储挂载，请确认路径是否正确"})

    mount_path = matched_storage['mount_path']

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
        
        # 2. 获取分享根目录文件列表，定位路径起点
        root_files = ali.get_share_file_list(share_token_obj, parent_file_id='root')
        
        full_path_list = [p for p in full_path.split('/') if p]
        
        parts_to_traverse = []
        
        # 查找 full_path 中哪个部分对应 Share Root 下的一个文件/文件夹
        match_index = -1
        for i, part in enumerate(full_path_list):
            for rf in root_files:
                if rf.name == part:
                    match_index = i
                    break
            if match_index != -1:
                break
        
        if match_index != -1:
            parts_to_traverse = full_path_list[match_index:]
        else:
            # 如果没找到匹配，尝试使用原来的相对路径逻辑作为 fallback
            # 或者直接报错。按照用户需求，这里应该能找到。
            # Fallback: 假设 mount_path 对应 root
            rel_path = full_path[len(mount_path):].strip('/')
            parts_to_traverse = [p for p in rel_path.split('/') if p]
            
            # 如果还是空，说明就是根目录
            if not parts_to_traverse and full_path == mount_path:
                 pass # Transfer root content logic below

        # 3. 逐层下钻 (Drill down)
        current_file_id = 'root'
        found_target = None
        
        if not parts_to_traverse:
             # 如果没有路径需要遍历，说明目标就是 Share Root
             # 我们构造一个虚拟对象代表 Root
             found_target = type('obj', (object,), {'name': full_path_list[-1] if full_path_list else "Root_Transfer", 'file_id': 'root', 'type': 'folder'})
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
        save_to_parent_id = ALI_TARGET_FOLDER_ID
        
        # 如果是文件：创建同名文件夹（去后缀），转存该文件
        if getattr(found_target, 'type', 'folder') == 'file':
            target_name = os.path.splitext(target_name)[0]
            transfer_file_ids = [found_target.file_id]
            
            # 创建目标目录
            try:
                new_folder = ali.create_folder(target_name, ALI_TARGET_FOLDER_ID)
                if new_folder:
                    save_to_parent_id = new_folder.file_id
                    ali.batch_share_file_saveto_drive(transfer_file_ids, share_token_obj, save_to_parent_id)
            except Exception as create_err:
                print(f"Create folder or transfer file failed: {create_err}")
                
        else:
            # 如果是文件夹（或 Root）：创建同名文件夹，递归转存
            try:
                new_folder = ali.create_folder(target_name, ALI_TARGET_FOLDER_ID)
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


if __name__ == '__main__':
    app.run(host=HOST, port=PORT)
