#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
斗鱼「一起看」直播源 M3U 生成器
=================================
功能：从斗鱼「一起看」分区采集正在直播的房间，自动生成标准 M3U 直播源文件。

用法：
    python generator.py                    # 生成 douyu_yqk.m3u（使用本地代理URL）
    python generator.py -o mylist.m3u      # 指定输出文件
    python generator.py --server 8080      # 启动 HTTP 代理服务器（实时解析流地址）
    python generator.py --direct           # 直接解析斗鱼真实流地址（会过期，仅供测试）
    python generator.py --proxy-url        # 生成指向本地代理的 M3U（推荐配合--server使用）

原理说明：
    1. 通过斗鱼 API 获取「一起看」(cate2Id=208) 分区的直播房间列表
    2. 方案A（推荐）：启动本地 HTTP 代理服务器，M3U 中的 URL 指向本机
       播放器请求时，服务器实时解析斗鱼真实流地址并 302 跳转
    3. 方案B（--direct）：直接生成包含真实 HLS 流地址的 M3U（地址会过期）

依赖安装：
    pip install requests
    pip install PyExecJS   (Windows 需先安装 Node.js)
"""

import argparse
import json
import os
import re
import sys
import time
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)

try:
    import execjs
except ImportError:
    print("[WARN] 未安装 execjs，实时解析功能不可用。")
    print("       请执行: pip install PyExecJS")
    print("       Windows 还需安装 Node.js: https://nodejs.org")
    execjs = None

# ============================================================
# 配置区域
# ============================================================

# 斗鱼「一起看」分区 ID (实际为 208，非旧版的 263)
CATE2_ID = 208

# 子分类过滤：None 表示不过滤，可取 "290"(陪看), "291"(聊天) 等
CATE3_FILTER = None

# 最大获取页数（防止无限循环）
# 每页约 120 个房间，默认 1 页 = 120 个频道
MAX_PAGES = 1

# 默认流代理模板（{room_id} 占位符）
# 此代理已失效，保留仅作兼容。推荐使用 --server 本地代理模式。
DEFAULT_PROXY_TEMPLATE = "https://live.ottiptv.cc/douyu/{room_id}"

# 请求头（模拟浏览器，避免被斗鱼反爬拦截）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyu.com/g_yqk",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# ============================================================
# 斗鱼真实流解析器（基于 github.com/wbt5/real-url）
# ============================================================

class DouYuResolver:
    """
    斗鱼直播流真实地址解析器。

    通过移动端页面获取 JS 混淆代码，执行 sign 计算后调用斗鱼 API，
    获取带防盗链的 HLS/FLV 真实流地址。
    """

    def __init__(self, rid):
        self.did = '10000000000000000000000000001501'
        self.rid = str(rid)
        self.s = requests.Session()
        self.s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://m.douyu.com/',
        })
        # 尝试获取移动端页面（用于 JS sign 解析），失败则跳过 JS 模式
        self.res = None
        try:
            self.res = self.s.get('https://m.douyu.com/' + str(rid), timeout=15).text
            result = re.search(r'rid":(\d{1,8}),"vipId', self.res)
            if result:
                self.rid = result.group(1)
            # 即使找不到 rid，也继续使用原始 rid 尝试 preview API
        except Exception as e:
            print(f"  [INFO] 无法获取移动端页面: {e}，将仅使用 preview API")

    @staticmethod
    def md5(data):
        return hashlib.md5(data.encode('utf-8')).hexdigest()

    def _get_pre(self):
        """获取预览流地址（无需完整 sign）"""
        url = 'https://playweb.douyucdn.cn/lapi/live/hlsH5Preview/' + self.rid
        data = {'rid': self.rid, 'did': self.did}
        t13 = str(int(time.time() * 1000))
        auth = DouYuResolver.md5(self.rid + t13)
        headers = {'rid': self.rid, 'time': t13, 'auth': auth}
        res = self.s.post(url, headers=headers, data=data, timeout=30).json()
        error = res['error']
        key = ''
        stream_url = ''
        if res.get('data'):
            rtmp_live = res['data']['rtmp_live']
            stream_url = res['data']['rtmp_url'] + '/' + rtmp_live
            m = re.search(r'(\d{1,8}[0-9a-zA-Z]+)_?\d{0,4}p?(.m3u8|/playlist)', rtmp_live)
            if m:
                key = m.group(1)
        return error, key, stream_url

    def _get_js(self):
        """通过执行 JS 计算完整 sign，获取高清流地址"""
        if execjs is None:
            raise Exception("execjs 未安装，无法执行 JS sign 计算")
        if self.res is None:
            raise Exception("无法获取移动端页面，跳过 JS 解析")

        result = re.search(r'(function ub98484234.*)\s(var.*)', self.res).group()
        func_ub9 = re.sub(r'eval.*;}', 'strc;}', result)
        js = execjs.compile(func_ub9)
        res = js.call('ub98484234')

        v = re.search(r'v=(\d+)', res).group(1)
        t10 = str(int(time.time()))
        rb = DouYuResolver.md5(self.rid + self.did + t10 + v)

        func_sign = re.sub(r'return rt;}\);?', 'return rt;}', res)
        func_sign = func_sign.replace('(function (', 'function sign(')
        func_sign = func_sign.replace('CryptoJS.MD5(cb).toString()', '"' + rb + '"')

        js = execjs.compile(func_sign)
        params = js.call('sign', self.rid, self.did, t10)
        params += '&ver=219032101&rid={}&rate=-1'.format(self.rid)

        url = 'https://m.douyu.com/api/room/ratestream'
        res = self.s.post(url, params=params, timeout=30).json()['data']
        m = re.search(r'(\d{1,8}[0-9a-zA-Z]+)_?\d{0,4}p?(.m3u8|/playlist)', res['url'])
        key = m.group(1) if m else ''
        return key, res['url']

    def get_real_url(self):
        """获取真实流地址，优先使用 JS 解析流（稳定 HD），回退到 preview 流。"""
        # 1. 优先尝试 JS 解析的完整流（更稳定，画质更高）
        try:
            _key, stream_url = self._get_js()
            if stream_url and ('.m3u8' in stream_url or '.flv' in stream_url):
                return stream_url
        except Exception as e:
            print(f"  [JS 解析失败: {e}]")

        # 2. 回退到 preview 流
        error, _key, preview_url = self._get_pre()
        if error == 0 and preview_url:
            return preview_url
        elif error == 102:
            raise Exception('房间不存在')
        elif error == 104:
            raise Exception('房间未开播')

        return ""


def resolve_douyu_stream(room_id):
    """
    解析斗鱼房间的真实直播流地址。
    返回可直接播放的 HLS/FLV URL，或 None（解析失败/未开播）。
    """
    try:
        resolver = DouYuResolver(room_id)
        url = resolver.get_real_url()
        if url and ('.m3u8' in url or '.flv' in url):
            return url
        return None
    except Exception as e:
        print(f"[WARN] 解析房间 {room_id} 失败: {e}")
        return None


# ============================================================
# 斗鱼 API 交互层（房间列表）
# ============================================================

def fetch_room_list(cate2_id=CATE2_ID, cate3_filter=CATE3_FILTER,
                    max_pages=MAX_PAGES):
    """
    从斗鱼 API 获取指定分区的房间列表。

    接口: GET https://www.douyu.com/gapi/rkc/directory/2_{cate2_id}/{page}
    返回字段: rid(房间号), rn(标题), nn(主播), av(头像), cid3(子分类)
    """
    rooms = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for page in range(1, max_pages + 1):
        url = f"https://www.douyu.com/gapi/rkc/directory/2_{cate2_id}/{page}"
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[WARN] 请求第{page}页失败: {e}")
            continue

        try:
            data = resp.json()
        except json.JSONDecodeError:
            print(f"[WARN] 第{page}页返回非JSON数据")
            continue

        if data.get("code") != 0:
            print(f"[WARN] 第{page}页 API 返回错误: {data.get('msg', 'unknown')}")
            continue

        page_data = data.get("data", {})
        room_list = page_data.get("rl", [])
        if not room_list:
            break

        for room in room_list:
            rid = room.get("rid")
            if cate3_filter is not None:
                if str(room.get("cid3", 0)) != str(cate3_filter):
                    continue

            nn = room.get("nn", "")
            av = room.get("av", "")
            avatar_url = f"https://apic.douyucdn.cn/upload/{av}_big.jpg" if av else ""

            rooms.append({
                "room_id": rid,
                "nickname": nn,
                "avatar": avatar_url,
            })

        print(f"[INFO] 第{page}页获取 {len(room_list)} 个房间"
              f"（过滤后 {len(rooms)} 个）")

        total_pages = page_data.get("pgcnt", 0)
        if page >= total_pages:
            break
        time.sleep(0.5)

    return rooms


# ============================================================
# M3U 生成层
# ============================================================

def generate_m3u(rooms, proxy_template=None, use_direct=False,
                 output_path=None, local_server_url=None, max_rooms=0,
                 worker_url=None):
    """
    生成 M3U 格式的直播源文件。

    Args:
        rooms: 房间列表，dict 包含 room_id, nickname, avatar
        proxy_template: 外部代理模板（如已失效的 ottiptv），{room_id} 占位符
        use_direct: 是否直接解析真实流地址（会过期，仅供测试）
        output_path: 输出文件路径，为 None 时仅返回内容
        local_server_url: 本地代理地址前缀，如 http://192.168.1.100:8080
                          生成 URL: {local_server_url}/douyu/{room_id}
        max_rooms: 最多解析的房间数量，0 表示不限制
        worker_url: Cloudflare Worker 代理地址前缀，如 https://xxx.workers.dev
                    生成 URL: {worker_url}/douyu/{room_id}
                    Worker 负责解析真实流并代理 HLS（添加 Referer）

    Returns:
        (valid_count, update_time, m3u_content)
    """
    if max_rooms > 0:
        rooms = rooms[:max_rooms]
        print(f"[INFO] 限制解析前 {len(rooms)} 个房间")

    update_time = time.strftime("%Y-%m-%d %H:%M:%S")
    default_avatar = "https://apic.douyucdn.cn/upload/avatar/default/08_big.jpg"

    lines = [
        "#EXTM3U",
        f'#EXTINF:-1 tvg-name="更新时间: {update_time}" '
        f'tvg-logo="{default_avatar}" group-title="列表信息",更新时间: {update_time}',
        "https://cdn.jsdelivr.net/gh/feiyang666999/testvideo/time/time.mp4",
        "",
    ]

    valid_count = 0
    total = len(rooms)
    for idx, room in enumerate(rooms, 1):
        rid = room["room_id"]
        nickname = room.get("nickname", f"房间{rid}")
        avatar = room.get("avatar", "") or default_avatar

        if use_direct:
            print(f"[{idx}/{total}] 解析房间 {rid}({nickname})...", end="", flush=True)
            stream_url = resolve_douyu_stream(rid)
            if not stream_url:
                print(f" 失败，跳过")
                continue
            print(f" 成功")
        elif local_server_url:
            # 指向本地代理服务器
            stream_url = f"{local_server_url}/douyu/{rid}"
        elif worker_url:
            # 指向 Cloudflare Worker 代理（推荐用于电视端）
            stream_url = f"{worker_url}/douyu/{rid}"
        elif proxy_template:
            # 外部代理（可能已失效）
            stream_url = proxy_template.format(room_id=rid)
        else:
            stream_url = f"https://www.douyu.com/{rid}"

        lines.append(
            f'#EXTINF:-1 tvg-logo="{avatar}" group-title="一起看", {nickname}'
        )
        lines.append(stream_url)
        lines.append("")
        valid_count += 1

    content = "\n".join(lines)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
    return valid_count, update_time, content


# ============================================================
# HTTP 代理服务器（实时流解析 + 302 跳转）
# ============================================================

class StreamProxyHandler(BaseHTTPRequestHandler):
    """
    本地 HTTP 代理处理器。

    提供端点：
      1. GET /playlist.m3u           → 返回 M3U 播放列表（URL 指向本机）
      2. GET /douyu/{room_id}        → 302 重定向到斗鱼真实 HLS 流地址
      3. GET /                       → 管理页面
    """

    server_base_url = "http://localhost:8080"
    rooms_cache = []
    cache_time = 0
    CACHE_TTL = 300  # 5 分钟
    stream_cache = {}  # room_id → (url, expire_time)
    STREAM_TTL = 3600  # 流地址缓存 1 小时

    def log_message(self, format, *args):
        print(f"[ACCESS] {self.client_address[0]} - {format % args}")

    def _get_rooms(self):
        now = time.time()
        if now - self.cache_time > self.CACHE_TTL:
            print("[SERVER] 刷新房间列表...")
            self.__class__.rooms_cache = fetch_room_list()
            self.__class__.cache_time = now
            print(f"[SERVER] 获取到 {len(self.rooms_cache)} 个房间")
        return self.rooms_cache

    def _resolve_stream(self, room_id):
        """获取流地址（带缓存）"""
        now = time.time()
        cached = self.stream_cache.get(room_id)
        if cached and now - cached[1] < self.STREAM_TTL:
            return cached[0]
        url = resolve_douyu_stream(room_id)
        if url:
            self.stream_cache[room_id] = (url, now)
        return url

    def _serve_m3u(self):
        rooms = self._get_rooms()
        valid_count, update_time, content = generate_m3u(
            rooms, local_server_url=self.server_base_url, output_path=None
        )
        self.send_response(200)
        self.send_header("Content-Type", "audio/x-mpegurl")
        self.send_header("Content-Length", len(content.encode("utf-8")))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _serve_stream_redirect(self, room_id):
        stream_url = self._resolve_stream(room_id)
        if stream_url:
            self.send_response(302)
            self.send_header("Location", stream_url)
            self.end_headers()
            print(f"[SERVER] 房间 {room_id} → {stream_url[:80]}...")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Stream not available")
            print(f"[SERVER] 房间 {room_id} 无可用流")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/playlist.m3u", "/douyu_yqk.m3u"):
            self._serve_m3u()
        elif path.startswith("/douyu/"):
            room_id = path.split("/")[-1]
            if room_id.isdigit():
                self._serve_stream_redirect(room_id)
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid room_id")
        elif path == "/":
            self._serve_index()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def _serve_index(self):
        local_ip = get_local_ip()
        port = self.server.server_port
        base = f"http://{local_ip}:{port}"
        rooms = self._get_rooms()
        cache_str = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(self.cache_time)
        ) if self.cache_time else "无"

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>斗鱼一起看 直播源</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 720px; margin: 40px auto; padding: 20px; background: #f5f5f5; }}
.card {{ background: #fff; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
h1 {{ color: #ff6d00; margin: 0 0 12px; font-size: 24px; }}
h2 {{ font-size: 16px; color: #333; border-left: 4px solid #ff6d00; padding-left: 10px; margin: 20px 0 12px; }}
code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 4px; font-size: 13px; word-break: break-all; }}
.url-box {{ background: #e8f4fd; border: 1px solid #b8d8f0; padding: 12px; border-radius: 8px; word-break: break-all; margin: 8px 0; font-size: 14px; }}
.status {{ color: #666; font-size: 13px; margin-top: 8px; }}
.tip {{ color: #888; font-size: 12px; margin-top: 16px; }}
</style></head>
<body>
<div class="card">
<h1>斗鱼「一起看」直播源代理</h1>
<p>实时采集斗鱼一起看分区直播房间，生成 M3U 播放列表。</p>
</div>

<div class="card">
<h2>M3U 播放列表（添加到电视/播放器）</h2>
<div class="url-box"><code>{base}/playlist.m3u</code></div>
<p class="status">当前缓存房间数: {len(rooms)} | 缓存时间: {cache_str}</p>
</div>

<div class="card">
<h2>单房间流代理</h2>
<div class="url-box"><code>{base}/douyu/&#123;房间号&#125;</code></div>
<p>示例: <code>{base}/douyu/5875025</code></p>
</div>

<div class="card">
<h2>使用方法</h2>
<ol>
<li>在 PotPlayer / TiviMate / IPTV Pro 等播放器中添加 M3U 地址</li>
<li>播放时，本机自动解析斗鱼真实流地址并跳转</li>
<li>保持本程序运行，播放器可随时切换频道</li>
</ol>
</div>

<p class="tip">本服务仅用于个人技术学习，请勿用于商业用途。</p>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()


def start_server(port=8080):
    """启动 HTTP 代理服务器"""
    local_ip = get_local_ip()
    StreamProxyHandler.server_base_url = f"http://{local_ip}:{port}"

    server = HTTPServer(("0.0.0.0", port), StreamProxyHandler)
    print(f"""
{'=' * 58}
     斗鱼「一起看」直播源代理服务器
{'=' * 58}
  M3U 播放列表:  http://{local_ip}:{port}/playlist.m3u
  管理页面:      http://{local_ip}:{port}/
  流代理:        http://{local_ip}:{port}/douyu/房间号
{'=' * 58}
  将 M3U 地址添加到播放器即可观看
  按 Ctrl+C 停止服务器
{'=' * 58}
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SERVER] 服务器已停止")
        server.shutdown()


def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ============================================================
# 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="斗鱼「一起看」直播源 M3U 生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
推荐用法（最稳定）：
  1. 启动服务器:   python generator.py --server 8080
  2. 播放器添加 M3U 地址: http://你的电脑IP:8080/playlist.m3u
  3. 保持程序运行，随时切换频道

其他用法:
  python generator.py                    # 生成 douyu_yqk.m3u（默认 120 个频道）
  python generator.py -o mylist.m3u      # 指定输出文件
  python generator.py --direct           # 直接解析真实流地址（会过期，仅供测试）
  python generator.py --proxy-url        # 生成指向本地代理的 M3U（配合 --server 使用）
  python generator.py --pages 5          # 获取更多频道（5页约600个）
  python generator.py --max-rooms 30     # 只解析前30个房间（快速测试）

依赖安装:
  pip install requests PyExecJS
  Windows 用户还需安装 Node.js: https://nodejs.org
        """,
    )
    parser.add_argument(
        "-o", "--output", default="douyu_yqk.m3u",
        help="输出 M3U 文件路径 (默认: douyu_yqk.m3u)",
    )
    parser.add_argument(
        "--server", type=int, metavar="PORT",
        help="启动 HTTP 代理服务器（指定端口，如 8080）",
    )
    parser.add_argument(
        "--direct", action="store_true",
        help="直接解析真实流地址（地址会过期，仅供测试）",
    )
    parser.add_argument(
        "--proxy-url", action="store_true",
        help="生成指向本地代理的 M3U URL（推荐配合 --server 使用）",
    )
    parser.add_argument(
        "--proxy", type=str, default=DEFAULT_PROXY_TEMPLATE,
        help="自定义外部流代理模板（已失效，不推荐）",
    )
    parser.add_argument(
        "--worker-url", type=str, default=None, metavar="URL",
        help="Cloudflare Worker 代理地址（如 https://xxx.workers.dev），"
             "M3U 中的频道 URL 将指向 Worker 代理以解决电视端 Referer 问题",
    )
    parser.add_argument(
        "--pages", type=int, default=MAX_PAGES,
        help=f"最大获取页数 (默认: {MAX_PAGES}，约{MAX_PAGES * 120}个频道)",
    )
    parser.add_argument(
        "--max-rooms", type=int, default=0, metavar="N",
        help="最多解析的房间数量 (默认: 0=不限制，建议设为 120)",
    )

    args = parser.parse_args()

    # 检查 execjs 是否可用
    if execjs is None:
        print("[WARN] execjs 未安装，JS sign 解析不可用。")
        print("       将使用 preview API 获取流地址（兼容但可能画质稍低）。")
        print("       如需完整功能: pip install PyExecJS")
        if not args.server and not args.direct:
            # 没有解析器，只能生成外部代理 URL
            print("\n将仅生成外部代理 URL 的 M3U（可能无法播放）。")
            print("建议安装依赖后使用 --server 本地代理模式。\n")

    # HTTP 服务器模式
    if args.server:
        start_server(port=args.server)
        return

    # M3U 生成模式
    print("=" * 50)
    print("  斗鱼「一起看」直播源生成器")
    print("=" * 50)
    print(f"[INFO] 开始获取房间列表...")

    rooms = fetch_room_list(cate2_id=CATE2_ID, max_pages=args.pages)

    if not rooms:
        print("[ERROR] 未能获取到任何房间，请检查网络连接。")
        sys.exit(1)

    print(f"\n[INFO] 共获取到 {len(rooms)} 个直播房间")

    if args.direct:
        print("[INFO] 直接解析真实流地址（会过期，仅供测试）...")
        valid_count, update_time, _ = generate_m3u(
            rooms, use_direct=True, output_path=args.output,
            max_rooms=args.max_rooms,
        )
    elif args.proxy_url:
        print("[INFO] 生成指向本地代理的 M3U URL...")
        print("[INFO] 请配合使用: python generator.py --server 8080")
        local_ip = get_local_ip()
        server_url = f"http://{local_ip}:8080"
        valid_count, update_time, _ = generate_m3u(
            rooms, local_server_url=server_url, output_path=args.output,
            max_rooms=args.max_rooms,
        )
    elif args.worker_url:
        worker_url = args.worker_url.rstrip("/")
        print(f"[INFO] 使用 Worker 代理: {worker_url}")
        print("[INFO] Worker 负责解析真实流并代理 HLS（添加 Referer）")
        print("[INFO] M3U 中的频道 URL 不会过期，Worker 实时解析。")
        valid_count, update_time, _ = generate_m3u(
            rooms, worker_url=worker_url, output_path=args.output,
            max_rooms=args.max_rooms,
        )
    else:
        print(f"[INFO] 使用外部代理: {args.proxy}")
        print("[WARN] 默认代理 live.ottiptv.cc 已失效，可能无法播放。")
        print("[INFO] 建议改用 --server 本地代理模式或 --direct 测试模式。")
        valid_count, update_time, _ = generate_m3u(
            rooms, proxy_template=args.proxy, output_path=args.output,
            max_rooms=args.max_rooms,
        )

    file_size = os.path.getsize(args.output)
    print(f"\n{'=' * 50}")
    print(f"  直播源生成完成!")
    print(f"  文件: {os.path.abspath(args.output)}")
    print(f"  频道: {valid_count} 个")
    print(f"  大小: {file_size:,} 字节")
    print(f"  时间: {update_time}")
    print(f"{'=' * 50}")
    print(f"\n将此文件导入播放器即可使用。")


if __name__ == "__main__":
    main()
