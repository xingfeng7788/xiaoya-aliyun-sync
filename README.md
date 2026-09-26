# 小雅资源助手

一站式小雅 Alist 资源管理工具，提供资源搜索、一键转存、阿里云盘管理、定时上传、Token 自动维护等功能。

## 功能特性

### 🔍 资源搜索与转存
- 关键词搜索小雅 Alist 资源库
- 搜索历史记录（本地存储）
- **一键转存**到阿里云盘，支持选择目标目录
- 自动解析 Alias 挂载重定向
- 递归转存整个文件夹结构

### ☁️ 阿里云盘管理
- 可视化文件浏览器（文件夹导航、面包屑）
- 上传文件到指定目录
- 下载文件到服务器本地目录（可选择下载路径）
- 创建文件夹 / 删除文件（移入回收站）
- 代理下载（解决 Referer 限制）

### 🔐 用户认证
- 基于 Session 的登录认证
- PBKDF2-SHA256 密码加密存储
- 首次启动自动创建 admin 账户（密码输出到控制台）
- 支持修改密码，不支持注册（安全优先）
- 全局路由保护，API 401 自动跳转登录页

### ⏰ 定时任务
- Cron 表达式调度（基于 APScheduler）
- 本地目录 → 阿里云盘定时上传
- 可视化本地目录浏览器 + 云盘目录选择器
- 任务启用/禁用、手动触发
- 执行日志查看（终端风格详情面板）
- **内置任务**：
  - Token 有效性检测（每 30 分钟）
  - Token 定时刷新（每天凌晨 4:00）

### 📱 消息推送
- PushPlus 微信通知集成
- Token 失效自动推送告警
- 5 分钟防抖，避免重复通知
- 支持群组推送

### 🔑 阿里云盘登录
- 网页扫码登录（二维码实时刷新）
- Token 自动持久化到数据库
- API 调用后自动同步最新 Token

### ⚙️ 配置管理
- SQLite 持久化存储
- 预设配置项提示（必填 / 选填分组显示）
- 支持环境变量初始化，数据库优先
- 在线编辑、新增、删除配置

## 快速开始

### 方式一：Docker 部署（推荐）

**1. 克隆项目**

```bash
git clone https://github.com/your-repo/xiaoya-helper.git
cd xiaoya-helper
```

**2. 创建环境配置**

```bash
cp example.env .env
```

编辑 `.env`，至少填写以下必填项：

```env
XIAOYA_URL=http://your-xiaoya-ip:5678
ADMIN_TOKEN=your-alist-admin-token
```

**3. 启动容器**

```bash
docker compose up -d
```

**4. 获取默认密码**

```bash
docker logs xiaoya-helper 2>&1 | grep "密码"
```

首次启动会自动创建 admin 账户，密码打印在日志中。

**5. 访问**

打开浏览器访问 `http://your-ip:5666`，使用 admin 账户登录。

### 方式二：直接运行

**环境要求**：Python 3.9+

```bash
# 安装依赖
pip install -r requirements.txt

# 创建配置
cp example.env .env
# 编辑 .env 填写必要配置

# 启动
python app.py
```

## 配置说明

| 配置项 | 必填 | 说明 | 示例 |
|--------|------|------|------|
| `XIAOYA_URL` | ✅ | 小雅 Alist 搜索地址 | `http://192.168.1.100:5678` |
| `ADMIN_TOKEN` | ✅ | Alist 管理员 Token | 从 Alist 后台获取 |
| `XIAOYA_EXTERNAL_URL` | ✅ | 外部访问地址（通知链接用） | `http://your-ip:5666` |
| `ALIST_URL` | 选填 | Alist API 地址 | `http://192.168.1.100:5234` |
| `ALI_REFRESH_TOKEN` | 选填 | 阿里云盘 Token（可网页扫码） | 扫码登录后自动保存 |
| `PUSHPLUS_TOKEN` | 选填 | PushPlus 通知 Token | 从 pushplus.plus 获取 |
| `PUSHPLUS_TOPIC` | 选填 | PushPlus 群组编码 | 一对多推送时使用 |
| `SECRET_KEY` | 选填 | Flask Session 密钥 | 留空则随机生成 |
| `HOST` | 选填 | 监听地址 | 默认 `0.0.0.0` |
| `PORT` | 选填 | 监听端口 | 默认 `5666` |

> 所有配置均可在页面「配置管理」中在线修改，数据库中的值优先于环境变量。

## Docker 部署详解

### 目录结构

```
xiaoya-helper/
├── .env                # 环境配置（从 example.env 复制）
├── data/               # 数据持久化（自动创建）
│   └── xiaoya.db       # SQLite 数据库
└── downloads/          # 云盘文件下载目录
```

### 自定义端口

修改 `.env` 中的 `PORT` 即可，`docker-compose.yaml` 会自动使用：

```env
PORT=8080
```

### 与小雅 Alist 同网络通信

如果小雅 Alist 也运行在 Docker 中，可以通过 Docker 网络直接通信，编辑 `docker-compose.yaml`：

```yaml
services:
  xiaoya-helper:
    # ...
    networks:
      - xiaoya-net

networks:
  xiaoya-net:
    external: true
```

然后 `.env` 中使用容器名访问：

```env
XIAOYA_URL=http://xiaoya-container-name:5678
ALIST_URL=http://xiaoya-container-name:5234
```

### 常用命令

```bash
# 启动
docker compose up -d

# 查看日志
docker compose logs -f

# 重启
docker compose restart

# 停止
docker compose down

# 重新构建（代码更新后）
docker compose up -d --build
```

## 使用指南

### 首次使用流程

1. **登录系统** → 查看控制台日志获取默认 admin 密码
2. **修改密码** → 点击右上角用户头像 → 修改密码
3. **配置管理** → 填写 `XIAOYA_URL` 和 `ADMIN_TOKEN`
4. **扫码登录** → 配置管理 → 扫码登录，用阿里云盘 APP 扫码
5. **搜索转存** → 搜索资源 → 选择目标目录 → 一键转存

### 定时上传任务

1. 点击导航栏「定时任务」
2. 点击「新建任务」
3. 选择本地目录（服务器上的目录）
4. 选择云盘目标目录
5. 设置 Cron 表达式（如 `0 2 * * *` 每天凌晨 2 点）
6. 选择同步模式：按日期全量备份，或 1:1 镜像同步（增量上传并将云端多余项移入回收站）
7. 保存并启用

> 镜像同步会让所选云端目录与本地目录保持一致，请使用专用目标目录，避免删除其中由其他方式保存的文件。

### Token 维护

系统内置两个自动任务，无需手动干预：

- **每 30 分钟**检测 Token 是否有效，失效则通过 PushPlus 推送通知
- **每天 04:00** 主动刷新 Token，延长有效期

如需手动更新，前往「配置管理」→「扫码登录」重新扫码即可。

## 项目结构

```
├── app.py              # Flask 主应用（路由、业务逻辑）
├── db.py               # SQLite 数据库模块（CRUD）
├── scheduler.py        # APScheduler 定时调度引擎
├── templates/
│   ├── index.html      # 主页面（单页应用）
│   └── login.html      # 登录页
├── aligo/              # 阿里云盘 SDK（本地副本）
├── Dockerfile          # 多阶段构建镜像
├── docker-compose.yaml # 容器编排
├── requirements.txt    # Python 依赖
└── example.env         # 环境变量模板
```

## 技术栈

- **后端**：Flask + SQLite + APScheduler
- **前端**：Tailwind CSS + Font Awesome（单页应用，无构建步骤）
- **SDK**：aligo（阿里云盘 Python SDK）
- **部署**：Docker + tini（信号处理）
- **安全**：PBKDF2-SHA256 密码哈希 / Session 认证 / 全局路由保护

## License

MIT
