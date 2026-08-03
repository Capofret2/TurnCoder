"""ChatApp 自动更新器 — 启动前检查版本并自动应用更新包。

版本格式：精确到秒的时间戳，如 20260424_120000
更新包：zip 文件，包含 update.py（更新脚本）和需要更新的文件。
update.py 负责覆写/新增/删除文件，最后删除更新包自身。

用法：python updater.py  （替代直接运行 python app.py）
"""
import json
import os
import sys
import time
import zipfile
import shutil
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(BASE_DIR, 'version.json')
UPDATE_DIR = os.path.join(BASE_DIR, 'data', 'updates')

# 更新源配置（可通过 version.json 中的 update_url 覆盖）
DEFAULT_UPDATE_URL = ''  # 留空时跳过远程检查


def get_local_version():
    """读取本地版本信息。返回 {'timestamp': '20260424_120000', ...}"""
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f'[UPDATER] 读取 version.json 失败: {e}')
    return {'timestamp': '19700101_000000', 'version': '0.0.0'}


def save_local_version(version_info):
    """保存版本信息到本地。"""
    try:
        with open(VERSION_FILE, 'w', encoding='utf-8') as f:
            json.dump(version_info, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'[UPDATER] 保存 version.json 失败: {e}')


def check_remote_version(update_url):
    """检查远程最新版本。支持 Gitee Releases API 和普通 HTTP 两种模式。

    Gitee 模式 update_url 格式: gitee://owner/repo?token=xxx
    普通 HTTP 模式: http://server:port/token
    """
    if not update_url:
        return None
    try:
        import requests
        if update_url.startswith('gitee://'):
            # Gitee Releases 模式
            parts = update_url.replace('gitee://', '').split('?')
            repo_path = parts[0]  # owner/repo
            token = ''
            # 优先从独立文件读取 token
            _token_path = os.path.join(BASE_DIR, 'data', 'update_admin_token.txt')
            if os.path.exists(_token_path):
                with open(_token_path, 'r') as _tf:
                    token = _tf.read().strip()
            if not token and len(parts) > 1:
                for param in parts[1].split('&'):
                    if param.startswith('token='):
                        token = param[6:]
            api_url = f'https://gitee.com/api/v5/repos/{repo_path}/releases/latest'
            # 公开仓库不需要 token 即可查询 Releases
            params = {}
            if token:
                params['access_token'] = token
            resp = requests.get(api_url, params=params, timeout=15, proxies={'http': None, 'https': None})
            if resp.status_code == 200:
                release = resp.json()
                # 从 release 的 body 中提取 version.json
                body = release.get('body', '')
                try:
                    import re
                    ver_match = re.search(r'```json\n(.*?)```', body, re.DOTALL)
                    if ver_match:
                        ver_data = json.loads(ver_match.group(1).strip())
                    else:
                        ver_data = {'timestamp': release.get('tag_name', ''), 'version': release.get('tag_name', '')}
                except Exception:
                    ver_data = {'timestamp': release.get('tag_name', ''), 'version': release.get('tag_name', '')}
                # 从 assets 中找到 zip 的下载链接
                assets = release.get('assets', [])
                for asset in assets:
                    if asset.get('name', '').endswith('.zip'):
                        dl_url = asset.get('browser_download_url', '')
                        if token:
                            dl_url += ('&' if '?' in dl_url else '?') + f'access_token={token}'
                        ver_data['download_url'] = dl_url
                        break
                ver_data['_gitee_release_id'] = release.get('id')
                return ver_data
            elif resp.status_code == 404:
                print('[UPDATER] Gitee 暂无 Release，跳过')
                return None
        else:
            # 普通 HTTP 模式
            resp = requests.get(f'{update_url}/version.json', timeout=10, proxies={'http': None, 'https': None})
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f'[UPDATER] 检查远程版本失败: {e}')
    return None


def download_update(download_url, save_path, base_url=''):
    """下载更新包到指定路径。download_url 可以是相对路径（相对于 base_url）或完整 URL。"""
    if not download_url.startswith('http'):
        download_url = f'{base_url}/{download_url}'
    try:
        import requests
        print(f'[UPDATER] 下载更新包: {download_url}')
        resp = requests.get(download_url, timeout=120, stream=True, proxies={'http': None, 'https': None})
        if resp.status_code != 200:
            print(f'[UPDATER] 下载失败: HTTP {resp.status_code}')
            return False
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f'[UPDATER] 下载完成: {os.path.getsize(save_path)} bytes')
        return True
    except Exception as e:
        print(f'[UPDATER] 下载异常: {e}')
        return False


def apply_update(zip_path):
    """解压更新包并执行 update.py 更新脚本。返回是否成功。"""
    extract_dir = zip_path + '_extracted'
    try:
        # 解压到临时目录
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
            print(f'[UPDATER] 解压完成: {len(zf.namelist())} 个文件')

        # 执行 update.py（如果存在）
        update_script = os.path.join(extract_dir, 'update.py')
        if os.path.exists(update_script):
            print('[UPDATER] 执行更新脚本 update.py ...')
            result = subprocess.run(
                [sys.executable, update_script, '--base-dir', BASE_DIR, '--extract-dir', extract_dir],
                capture_output=True, text=True, timeout=60
            )
            print(f'[UPDATER] update.py stdout: {result.stdout}')
            if result.returncode != 0:
                print(f'[UPDATER] update.py 失败: {result.stderr}')
                return False
        else:
            # 没有 update.py，直接覆盖文件
            print('[UPDATER] 无 update.py，直接覆写文件...')
            for root, dirs, files in os.walk(extract_dir):
                for fname in files:
                    src = os.path.join(root, fname)
                    rel = os.path.relpath(src, extract_dir)
                    dst = os.path.join(BASE_DIR, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    print(f'  覆写: {rel}')

        return True
    except Exception as e:
        print(f'[UPDATER] 应用更新失败: {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        # 清理临时文件
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        if os.path.exists(zip_path):
            os.remove(zip_path)
            print(f'[UPDATER] 已清理更新包: {zip_path}')


def main():
    """主入口：检查更新 → 应用更新 → 启动 app.py"""
    print('=' * 50)
    print('ChatApp 启动器 (含自动更新)')
    print('=' * 50)

    local_ver = get_local_version()
    print(f'[UPDATER] 本地版本: {local_ver.get("timestamp", "未知")} ({local_ver.get("version", "?")})')

    # 检查远程版本
    update_url = local_ver.get('update_url', DEFAULT_UPDATE_URL)
    if update_url:
        remote_ver = check_remote_version(update_url)
        if remote_ver and remote_ver.get('timestamp', '') > local_ver.get('timestamp', ''):
            print(f'[UPDATER] 发现新版本: {remote_ver["timestamp"]} ({remote_ver.get("version", "?")})')
            download_url = remote_ver.get('download_url', '')
            if download_url:
                zip_name = f'update_{remote_ver["timestamp"]}.zip'
                zip_path = os.path.join(UPDATE_DIR, zip_name)
                if download_update(download_url, zip_path, base_url=update_url):
                    if apply_update(zip_path):
                        # 更新成功，保存新版本号
                        save_local_version(remote_ver)
                        print(f'[UPDATER] 更新成功！新版本: {remote_ver["timestamp"]}')
                        # 原先是 os.execv 自我重启。POSIX 上它替换进程映像、PID 不变，
                        # 递归检查下一个更新是安全的；Windows 上没有这个语义，CPython
                        # 的实现是新起一个进程再让原进程立即退出——父进程句柄失效、
                        # 控制台归属混乱，在被 shell 启动的场景下表现是「命令看起来
                        # 结束了但服务在后台继续跑」。
                        # 改为打印并退出：行为在两个平台上一致且可预测。退出码 0 表示
                        # 更新本身成功；若有外层脚本按「0 就自动重跑」包着它会变成循环，
                        # 所以提示文字明确要求人来重启。
                        print('[UPDATER] 更新已应用，请手动重启。')
                        sys.exit(0)
                    else:
                        print('[UPDATER] 更新应用失败，使用当前版本启动')
                else:
                    print('[UPDATER] 下载失败，使用当前版本启动')
            else:
                print('[UPDATER] 远程版本无下载链接，跳过更新')
        elif remote_ver:
            print('[UPDATER] 当前已是最新版本')
        else:
            print('[UPDATER] 无法连接更新服务器，跳过检查')
    else:
        print('[UPDATER] 未配置更新源 (update_url)，跳过远程检查')

    # 检查本地更新包（手动放置的更新包）
    if os.path.exists(UPDATE_DIR):
        for fname in sorted(os.listdir(UPDATE_DIR)):
            if fname.endswith('.zip') and fname.startswith('update_'):
                zip_path = os.path.join(UPDATE_DIR, fname)
                print(f'[UPDATER] 发现本地更新包: {fname}')
                if apply_update(zip_path):
                    # 从文件名提取时间戳作为版本
                    ts = fname.replace('update_', '').replace('.zip', '')
                    local_ver['timestamp'] = ts
                    save_local_version(local_ver)
                    print(f'[UPDATER] 本地更新包应用成功: {ts}')
                else:
                    print(f'[UPDATER] 本地更新包应用失败: {fname}')

    # 启动 ChatApp
    print('\n[UPDATER] 启动 ChatApp...')
    print('=' * 50)
    os.chdir(BASE_DIR)
    os.execv(sys.executable, [sys.executable, os.path.join(BASE_DIR, 'app.py')])


if __name__ == '__main__':
    main()
