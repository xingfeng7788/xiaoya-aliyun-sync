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
    for line in XIAOYA_INDEX:
        if keyword.lower() in line.lower():
            # 1. 提取实际路径（去掉 # 后的元数据）
            full_path = line.split('#')[0]
            # 2. 清理显示名称（去掉开头的 .）
            display_name = full_path
            if display_name.startswith('.'):
                display_name = display_name[1:]
            
            results.append({
                "name": display_name,  # 显示用的路径
                "path": line           # 原始完整行，传给 transfer 接口
            })
            count += 1
        if count >= 100: break

    return jsonify({"status": "success", "results": results})


def get_file_list(path):
    """调用 Alist API 获取目录下文件列表"""
    payload = {
        "path": path,
        "password": "",
        "page": 1,
        "per_page": 0,
        "refresh": False
    }
    try:
        res = requests.post(f"{ALIST_URL}/api/fs/list", json=payload, 
                          headers={'Authorization': ADMIN_TOKEN})
        data = res.json()
        if data.get('code') == 200 and data.get('data') and data['data'].get('content'):
            return [item['name'] for item in data['data']['content']]
    except Exception as e:
        print(f"List files error: {e}")
    return None

@app.route('/api/transfer', methods=['POST'])
def transfer():
    # 原始路径处理
    raw_path = request.json.get('path', '')
    clean_path = raw_path.split('#')[0]
    if clean_path.startswith('.'):
        clean_path = clean_path[1:]
    if not clean_path.startswith('/'):
        clean_path = '/' + clean_path
    clean_path = clean_path.rstrip('/')

    folder_name = clean_path.split('/')[-1]
    if not folder_name:
         return jsonify({"status": "error", "message": "无法解析文件名"})

    print(f"Transferring: {clean_path}")

    # --- 智能穿透策略 ---
    # 1. 尝试列出该路径下的内容
    # 如果能列出内容，说明是文件夹，且我们获取到了具体文件列表
    # 这样我们可以直接复制“里面的东西”，而不是复制“文件夹本身”，规避虚拟目录问题
    children_names = get_file_list(clean_path)
    
    headers = {'Authorization': ADMIN_TOKEN}

    if children_names:
        # 策略 A: 是文件夹，且获取到了内容 -> 复制内容到目标文件夹
        print(f"Smart Mode: Found {len(children_names)} items inside. Copying content directly.")
        
        # 目标路径需要加上文件夹名，例如 /我的网盘/来自小雅/遮天
        target_dst_dir = os.path.join(DEST_PATH, folder_name)
        
        payload = {
            "src_dir": clean_path,       # 源目录就是用户点的这个文件夹
            "names": children_names,     # 复制里面的所有文件名
            "dst_dir": target_dst_dir    # 目标目录是设定的存盘路径+文件夹名
        }
    else:
        # 策略 B: 是文件，或者空文件夹，或者列目录失败 -> 只能按原方式复制本体
        print("Standard Mode: Copying item itself.")
        src_dir = os.path.dirname(clean_path)
        payload = {
            "src_dir": src_dir,
            "names": [folder_name],
            "dst_dir": DEST_PATH
        }

    print(f"Payload: {payload}")

    try:
        # 注意：如果目标文件夹不存在，Alist 的 Copy API 通常会自动创建，
        # 但如果是深层目录可能需要确保父级存在。通常 Alist 处理得很好。
        response = requests.post(f"{ALIST_URL}/api/fs/copy", json=payload, headers=headers)
        
        try:
            res_json = response.json()
            print(f"Alist Response: {res_json}")
        except ValueError:
            print(f"Error: Alist response is not JSON. Status: {response.status_code}")
            return jsonify({"status": "error", "message": f"Alist API error: {response.status_code}"})

        if res_json.get('code') == 200:
            msg = f"转存任务已提交！包含 {len(children_names) if children_names else 1} 个项目。"
            msg += " 请在 Alist 后台【管理-任务】中查看进度。"
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"status": "error", "message": res_json.get('message', 'Unknown error')})

    except Exception as e:
        print(f"Exception during transfer: {str(e)}")
        return jsonify({"status": "error", "message": str(e)})


if __name__ == '__main__':
    load_index()  # 启动前加载
    app.run(host=HOST, port=PORT)