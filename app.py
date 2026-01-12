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
                results.append({"name": candidate.split('/')[-1], "path": candidate})

        return jsonify({"status": "success", "results": results[:100]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/api/transfer', methods=['POST'])
def transfer():
    full_path = request.json.get('path', '').split('#')[0]
    # 规范化路径
    if full_path.startswith('.'): full_path = full_path[1:]
    if not full_path.startswith('/'): full_path = '/' + full_path
    full_path = full_path.rstrip('/')

    # 1. 获取所有存储并匹配
    storages = get_alist_storages()
    matched_storage = None
    mount_path = ""

    # 寻找最长匹配的挂载路径
    for s in storages:
        m_path = s.get('mount_path')
        if not m_path: continue
        if full_path == m_path or full_path.startswith(m_path + '/'):
            if len(m_path) > len(mount_path):
                mount_path = m_path
                matched_storage = s

    if not matched_storage:
        return jsonify({"status": "error", "message": "未找到对应的存储挂载，请确认路径是否正确"})

    # 2. 从 addition 解析分享信息
    try:
        addition = json.loads(matched_storage.get('addition', '{}'))
    except:
        return jsonify({"status": "error", "message": "无法解析存储配置信息"})

    share_id = addition.get('share_id')
    share_pwd = addition.get('share_pwd', '')
    # root_folder_id 可能是 'root' 或者具体的 ID
    root_folder_id = 'root'
    full_path_list = full_path.split('/')
    if not share_id:
        return jsonify({"status": "error", "message": "该路径不是阿里云盘分享挂载 (未找到 share_id)"})

    try:
        ali = get_ali()

        # 3. 获取 Share Token
        share_token_obj = ali.get_share_token(share_id, share_pwd=share_pwd)
        share_token = share_token_obj.share_token

        # 4. 递归查找目标文件/文件夹的 file_id
        # 计算相对路径 parts
        rel_path = full_path[len(mount_path):].strip('/')
        parts = [p for p in rel_path.split('/') if p]

        root_folders = ali.get_share_file_list(share_token_obj, parent_file_id="root")
        parts = []
        for root_folder in root_folders:
            if root_folder.name in full_path_list:
                parts = full_path_list[full_path_list.index(root_folder.name):]
        current_file_id = root_folder_id
        found = None
        for i, part in enumerate(parts):
            # 获取当前目录下的文件列表
            # 注意: 这里的 parent_file_id 是在分享中的 ID
            files = ali.get_share_file_list(share_token_obj, parent_file_id=current_file_id)

            for f in files:
                if f.name == part:
                    current_file_id = f.file_id
                    if part == parts[-1]:
                        found = f
                    break

        # 5. 执行转存
        # current_file_id 即为目标资源的 ID
        share_file_list = ali.get_share_file_list(share_token_obj,
                                                  parent_file_id=current_file_id)
        batch_save_file = ali.batch_share_file_saveto_drive([i.file_id for i in share_file_list],
                                                            share_token_obj, ALI_TARGET_FOLDER_ID)

        return jsonify({
            "status": "success",
            "message": f"成功提交转存任务！资源 [{parts[-1] if parts else mount_path}] 已保存。"
        })

    except Exception as e:
        traceback.print_exc()
        print(f"Transfer error: {e}")
        return jsonify({"status": "error", "message": f"转存失败: {str(e)}"})


if __name__ == '__main__':
    app.run(host=HOST, port=PORT)
