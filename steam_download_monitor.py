import argparse
import os
import re
import threading
import time
from datetime import datetime
import winreg
import msvcrt


rate_re = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*Current download rate:\s*([0-9.]+)\s*Mbps"
)
inc_re = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*\(rate was\s*[0-9.]+,\s*now\s*([0-9.]+)\)"
)
stats_re = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*stats:\s*\(Invalid,\s*0\)\s*:\s*([0-9]+)\s*Bytes,\s*([0-9]+)\s*sec\s*\(([0-9.]+)\s*Mbps\)\."
)

upd_changed_re = re.compile(r"AppID\s+(\d+)\s+.*update changed\s*:\s*(.*)")
upd_started_re = re.compile(r"AppID\s+(\d+)\s+update started\s*:")
upd_canceled_re = re.compile(r"AppID\s+(\d+)\s+update canceled\s*:")
finished_re = re.compile(r"AppID\s+(\d+)\s+finished update")
state_changed_re = re.compile(r"AppID\s+(\d+)\s+state changed\s*:\s*(.*)")

name_re = re.compile(r'"\s*name\s*"\s*"([^"]+)"', re.IGNORECASE)


def read_steam_root():
    tries = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam", "InstallPath"),
    ]
    for root, subkey, value in tries:
        try:
            with winreg.OpenKey(root, subkey) as k:
                v, _ = winreg.QueryValueEx(k, value)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        except OSError:
            pass
    return None


def lib_paths(steam_root):
    libs = []

    def norm(p):
        p = p.replace("\\\\", "\\").strip().strip('"')
        return os.path.normpath(p)

    if os.path.isdir(os.path.join(steam_root, "steamapps")):
        libs.append(steam_root)

    vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    if not os.path.isfile(vdf):
        return list(dict.fromkeys(libs))

    try:
        txt = open(vdf, "r", encoding="utf-8", errors="ignore").read()
    except OSError:
        return list(dict.fromkeys(libs))

    for m in re.finditer(r'"\s*path\s*"\s*"([^"]+)"', txt, re.IGNORECASE):
        p = norm(m.group(1))
        if os.path.isdir(os.path.join(p, "steamapps")):
            libs.append(p)

    for m in re.finditer(r'"\s*\d+\s*"\s*"([^"]+)"', txt):
        p = norm(m.group(1))
        if os.path.isdir(os.path.join(p, "steamapps")):
            libs.append(p)

    return list(dict.fromkeys(libs))


def manifest_name(libs, appid):
    fname = f"appmanifest_{appid}.acf"
    for lib in libs:
        p = os.path.join(lib, "steamapps", fname)
        if not os.path.isfile(p):
            continue
        try:
            data = open(p, "r", encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        m = name_re.search(data)
        if m:
            return m.group(1).strip()
    return None


class State:
    def __init__(self):
        self.active_app = None
        self.active_status = ""
        self.app_status = {}
        self.seen = {}

        self.speed = None
        self.speed_wall_ts = 0.0
        self.speed_log_ts = ""
        self.speed_src = ""


def nice_status(raw):
    s = raw.lower()
    if "suspended" in s:
        return "Пауза"
    if "downloading" in s:
        return "Скачивание"
    if "staging" in s:
        return "Стадия (staging)"
    if "committing" in s:
        return "Установка (commit)"
    if "verifying" in s:
        return "Проверка"
    if "reconfiguring" in s:
        return "Подготовка"
    if "preallocating" in s:
        return "Выделение места"
    return "Нет"


def pick_active(st):
    if st.active_app:
        return st.active_app

    best = None
    for appid, ts in st.seen.items():
        ss = st.app_status.get(appid, "")
        if ss and ss not in ("Нет", "Готово"):
            if best is None or ts > best[0]:
                best = (ts, appid)
    return best[1] if best else None


def _set_speed(st, mbps, log_ts, src):
    st.speed = mbps
    st.speed_wall_ts = time.time()
    st.speed_log_ts = log_ts
    st.speed_src = src


def parse_line(line, st):
    m = upd_changed_re.search(line)
    if m:
        appid = m.group(1)
        raw = m.group(2)
        ss = nice_status(raw)
        st.app_status[appid] = ss
        st.seen[appid] = time.time()

        if any(k in raw.lower() for k in ["downloading", "staging", "committing", "verifying", "preallocating", "reconfiguring"]):
            st.active_app = appid
            st.active_status = ss

        if raw.strip().lower() == "none":
            st.app_status[appid] = "Нет"
        return

    m = upd_started_re.search(line)
    if m:
        appid = m.group(1)
        st.active_app = appid
        st.active_status = "Скачивание"
        st.app_status[appid] = "Скачивание"
        st.seen[appid] = time.time()
        return

    m = upd_canceled_re.search(line)
    if m:
        appid = m.group(1)
        st.app_status[appid] = "Пауза"
        st.seen[appid] = time.time()
        if st.active_app == appid:
            st.active_status = "Пауза"
        return

    m = finished_re.search(line)
    if m:
        appid = m.group(1)
        st.app_status[appid] = "Готово"
        st.seen[appid] = time.time()
        if st.active_app == appid:
            st.active_app = None
            st.active_status = ""
            st.speed = None
            st.speed_wall_ts = 0.0
        return

    m = state_changed_re.search(line)
    if m:
        appid = m.group(1)
        raw = m.group(2)
        if "suspended" in raw.lower():
            st.app_status[appid] = "Пауза"
            st.seen[appid] = time.time()
            if st.active_app == appid:
                st.active_status = "Пауза"
        return

    m = rate_re.search(line)
    if m:
        _set_speed(st, float(m.group(2)), m.group(1), "rate")
        return

    m = inc_re.search(line)
    if m:
        _set_speed(st, float(m.group(2)), m.group(1), "inc")
        return

    m = stats_re.search(line)
    if m:
        _set_speed(st, float(m.group(4)), m.group(1), "stats")
        return


def watch_log(log_path, libs, every_sec, st, stop_ev):
    f = None
    pos = 0

    def reopen_tail():
        nonlocal f, pos
        if f:
            try:
                f.close()
            except Exception:
                pass

        f = open(log_path, "r", encoding="utf-8", errors="ignore")
        size = os.path.getsize(log_path)
        start = max(0, size - 200000)
        f.seek(start, os.SEEK_SET)

        if start > 0:
            f.readline()

        for ln in f:
            parse_line(ln.strip(), st)

        pos = f.tell()

    try:
        reopen_tail()
    except OSError as e:
        print(f"Не могу открыть content_log: {log_path}\n{e}")
        return

    last_out = 0.0
    stale_sec = 300

    while not stop_ev.is_set():
        try:
            size = os.path.getsize(log_path)
        except OSError:
            time.sleep(0.2)
            continue

        if size < pos:
            try:
                reopen_tail()
            except OSError:
                time.sleep(0.2)
                continue

        ln = f.readline()
        if not ln:
            time.sleep(0.05)
        else:
            pos = f.tell()
            parse_line(ln.strip(), st)

        now = time.time()
        if now - last_out < every_sec:
            continue
        last_out = now

        out_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        appid = pick_active(st)
        speed_ok = st.speed is not None and (time.time() - st.speed_wall_ts) <= stale_sec

        if not appid:
            print(f"[{out_time}] Ничего не загружается")
            continue

        name = manifest_name(libs, appid)
        if not name:
            print(f"[{out_time}] Ничего не загружается")
            continue
        ss = st.app_status.get(appid, st.active_status) or "?"

        if ss == "Пауза":
            print(f"[{out_time}] {name} | {ss}")
            continue

        if not speed_ok:
            print(f"[{out_time}] {name} | {ss} | скорость: ?")
        else:
            print(f"[{out_time}] {name} | {ss} | скорость: {st.speed:.3f} Mbps | ")

    try:
        if f:
            f.close()
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-i", type=float, default=60.0, help="Интервал вывода (сек)")
    args = p.parse_args()

    steam_root = read_steam_root()
    if not steam_root:
        print("Steam не найден в реестре.")
        return

    log_path = os.path.join(steam_root, "logs", "content_log.txt")
    if not os.path.isfile(log_path):
        print(f"Не найден файл: {log_path}")
        return

    libs = lib_paths(steam_root)

    stop_ev = threading.Event()
    st = State()

    th = threading.Thread(target=watch_log, args=(log_path, libs, args.i, st, stop_ev), daemon=True)
    th.start()

    print(f"Мониторинг: {log_path}")
    print(f"Интервал вывода: {args.i} сек")
    print("Нажми 'q' или Enter для остановки.")

    t0 = time.time()
    while True:
        if time.time() - t0 >= 300:
            break
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"q", b"Q", b"\r"):
                break
        time.sleep(0.03)

    stop_ev.set()
    th.join(timeout=1.0)
    print("Остановлено.")


if __name__ == "__main__":
    main()
