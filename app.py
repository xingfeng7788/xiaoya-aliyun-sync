import os
import re
from flask import Flask, render_template, request, jsonify
import requests
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

app = Flask(__name__)

# --- 配置信息 ---
ALIST_URL = os.getenv("ALIST_URL", "http://localhost:5244")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DEST_PATH = os.getenv("DEST_PATH", "/")
# 指向你宿主机上 index.txt 的路径 (如果在容器内运行，需挂载此文件)
INDEX_FILE_PATH = os.getenv("INDEX_FILE_PATH", "/etc/xiaoya/index.txt")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 5000))

# 全局变量：存储内存索引
XIAOYA_INDEX = []


def load_index():
    """程序启动时将几十万行索引加载进内存"""
    global XIAOYA_INDEX
    if os.path.exists(INDEX_FILE_PATH):
        print("正在加载本地索引...")
        with open(INDEX_FILE_PATH, 'r', encoding='utf-8') as f:
            # 过滤掉空行
            XIAOYA_INDEX = [line.strip() for line in f if line.strip()]
        print(f"索引加载完成，共 {len(XIAOYA_INDEX)} 条资源。")
    else:
        print(f"未找到索引文件: {INDEX_FILE_PATH}，将降级使用 API 搜索。")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/search', methods=['POST'])
def search():
    keyword = request.json.get('keyword', '')
    if not XIAOYA_INDEX:
        # 如果没加载成功索引，可以尝试降级回 API 搜索逻辑（略）
        return jsonify({"status": "error", "message": "索引未加载"})

    # 使用正则或简单的字符串包含进行模糊匹配
    # 限制返回前 100 条，防止前端卡死
    results = []
    count = 0
    for path in XIAOYA_INDEX:
        if keyword.lower() in path.lower():
            name = path.split('/')[-1]  # 取路径最后一部分作为名称
            results.append({"name": name, "path": path})
            count += 1
        if count >= 100: break

    return jsonify({"status": "success", "results": results})


@app.route('/api/transfer', methods=['POST'])
def transfer():
    # 原始路径，可能包含 # 后的元数据
    raw_path = request.json.get('path', '')
    
    # 1. 清理路径：去除 # 及之后的内容
    clean_path = raw_path.split('#')[0]
    
    # 2. 格式化路径：确保以 / 开头 (去掉开头的 . )
    if clean_path.startswith('.'):
        clean_path = clean_path[1:]
    if not clean_path.startswith('/'):
        clean_path = '/' + clean_path

    print(f"Transferring: {clean_path}") # Debug log

    name = clean_path.split('/')[-1]
    src_dir = os.path.dirname(clean_path)

    payload = {
        "src_dir": src_dir,
        "src_filenames": [name],
        "dst_dir": DEST_PATH
    }
    
    headers = {'Authorization': ADMIN_TOKEN}
    
    try:
        response = requests.post(f"{ALIST_URL}/api/fs/copy", json=payload, headers=headers)
        
        # 尝试解析 JSON
        try:
            res_json = response.json()
        except ValueError:
            # 如果不是 JSON，打印原始内容并报错
            print(f"Error: Alist response is not JSON. Status: {response.status_code}")
            print(f"Response text: {response.text}")
            return jsonify({"status": "error", "message": f"Alist API error: {response.status_code} - {response.text[:200]}"})

        if res_json.get('code') == 200:
            return jsonify({"status": "success", "message": "转存指令已发送"})
        else:
            return jsonify({"status": "error", "message": res_json.get('message', 'Unknown error')})

    except Exception as e:
        print(f"Exception during transfer: {str(e)}")
        return jsonify({"status": "error", "message": str(e)})


if __name__ == '__main__':
    load_index()  # 启动前加载
    app.run(host=HOST, port=PORT)