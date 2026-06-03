# ShareVideo — DLNA 媒体服务器

将 Windows 电脑上的视频共享到索尼电视（或其他 DLNA 电视）上播放。

## 工作原理

```
┌─── 电脑 (Windows 11) ───────────────────┐       ┌── 电视 (索尼 X90) ──┐
│                                          │       │                     │
│  Flask Web UI :8080                      │       │  自带媒体播放器      │
│  管理共享文件夹                            │       │  (DLNA 客户端)      │
│                                          │       │                     │
│  DLNA HTTP Server :8000                  │       │                     │
│  ├─ SSDP 多播广播 ─────────────────────────┼─►     │ 自动发现服务器       │
│  ├─ device.xml (设备描述)                  │       │                     │
│  ├─ SOAP Browse (浏览文件夹)               │       │                     │
│  └─ HTTP Range (视频流)                   │       │                     │
│                                          │       │                     │
│  SSDP UDP :1900                          │       │                     │
└──────────────────────────────────────────┘       └─────────────────────┘
             同一局域网（电脑网线 / 电视 WiFi 或网线）
```

电脑和电视在同一个路由器下即可，不需要任何线缆直连。

## 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| 语言 | Python 3 | 标准库为主 |
| Web 管理界面 | Flask | 唯一外部依赖 |
| DLNA 协议 | 标准库 `http.server` | 设备描述 + SOAP + 媒体服务 |
| 网络发现 | 标准库 `socket` | UDP 多播 SSDP |
| XML 处理 | 标准库 `xml.etree` | DIDL-Lite / SOAP 封装 |

**依赖极简**：只需要 `flask` 一个 pip 包，其他全部用 Python 标准库。

## 项目结构

```
sharevideao/
├── server.py              # 主入口：启动、路由、生命周期管理
├── start.bat              # Windows 双击启动脚本
├── requirements.txt       # flask
├── config.json            # 自动生成，持久化配置（UUID、端口、文件夹列表）
├── README.md              # 本文件
│
├── dlna/                  # DLNA 协议实现
│   ├── __init__.py
│   ├── device_xml.py      # 设备描述 XML + SCPD 服务描述 XML
│   ├── media_store.py     # 文件扫描、DIDL-Lite 生成、HTTP Range 媒体服务
│   ├── soap_handler.py    # ContentDirectory:1 + ConnectionManager:1 SOAP 处理
│   └── ssdp.py            # SSDP 多播广播 + M-SEARCH 响应 + byebye 通知
│
└── web/                   # Web 管理界面
    ├── __init__.py
    ├── app.py             # Flask API（文件夹增删、状态查询、重新扫描）
    └── templates/
        └── index.html     # 单页管理界面（纯 HTML + CSS + fetch()）
```

## 快速开始

### 1. 安装依赖

```bash
pip install flask
```

或直接双击 `start.bat`（自动安装）。

### 2. 启动服务器

```bash
python server.py
```

启动信息示例：

```
============================================================
  ShareVideo DLNA Media Server
============================================================
  Server name : ShareVideo
  Local IP    : 192.168.1.100
  DLNA port   : 8000
  Web UI      : http://192.168.1.100:8080
  Config      : config.json
------------------------------------------------------------
  SOAP handler: ready
  SSDP        : broadcasting
  Web UI      : http://localhost:8080
------------------------------------------------------------
  Press Ctrl+C to stop the server
============================================================
```

### 3. 打开管理界面

浏览器访问：**http://localhost:8080**

### 4. 添加视频文件夹

在 Web 界面输入文件夹路径（如 `C:\Users\你的用户名\Videos`），点击「添加」。

### 5. 在电视上播放

- 打开索尼电视自带的「**媒体播放器**」或「**视频**」应用
- 选择「**网络设备**」
- 在设备列表中看到 **ShareVideo**
- 浏览文件夹，选择视频直接播放

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | 8000 | DLNA HTTP 服务端口 |
| `--web-port` | 8080 | Web 管理界面端口 |
| `--config` | config.json | 配置文件路径 |

## 注意事项

- 电脑和电视必须在**同一个局域网**（同一个路由器下）
- 首次运行 Windows 防火墙会弹窗，**必须点击允许** Python 访问网络
- 按 `Ctrl+C` 停止服务器（会发送 byebye 通知电视下线）
- UUID 持久化在 config.json 中，不要随意删除
- 索尼电视原生支持 MP4、AVI、MKV 等格式
- 如果某个文件无法播放，可能需要转码为 MP4（H.264 编码）

## config.json 说明

```json
{
  "uuid": "自动生成的唯一标识",
  "server_name": "ShareVideo",
  "port": 8000,
  "web_port": 8080,
  "shared_folders": [
    "C:\\Users\\User\\Videos"
  ],
  "allowed_extensions": [
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"
  ]
}
```

## DLNA 协议实现细节

### 发现流程 (SSDP)

1. **服务器启动**：立即发送 3 轮 NOTIFY 广播（让已开机的电视快速发现）
2. **定期广播**：每 900 秒发送一次 NOTIFY（max-age=1800 秒）
3. **M-SEARCH**：电视开机扫描时发送多播查询，服务器单播回复
4. **关闭通知**：Ctrl+C 停止时发送 byebye NOTIFY（3 轮），电视立即感知下线

### 浏览流程 (SOAP)

1. **GetProtocolInfo**：电视查询支持的视频格式
2. **Browse(root)**：电视获取共享文件夹列表
3. **Browse(folder)**：用户进入子文件夹
4. **BrowseMetadata(video)**：电视获取视频 URL 和元数据
5. **HTTP GET**：电视请求视频文件（带 Range 头，支持拖拽 seek）

### 不需要的 DLNA 功能

- ~~GENA 事件订阅~~（索尼电视基本浏览不需要）
- ~~Search 搜索~~（电视用 Browse 就够了）
- ~~CreateObject / DestroyObject~~（只读模式）
- ~~转码~~（电视原生解码）
