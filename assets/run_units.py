#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_units.py：机械运行单元清单。仅标准库，Python 3.8 以上。

子命令
  run     按固定并发运行 units.jsonl 里的单元；产物文件已存在的单元跳过（恢复）；失败按固定次数重试，
          用完即隔离该单元，整批不停；进度写入 PROGRESS.md。加 --detach 则在后台运行并立即返回。
  status  打印一行状态。--wait N 最多阻塞 N 秒，期间离开 RUNNING 即提前返回。
  stop    请求运行进程不再启动新单元；运行中的单元跑完后进程退出。
  list    列出单元及当前状态。

units.jsonl 每行一个对象：
  {"id": "u001",                        唯一标识
   "cmd": "python run.py --case 1",     原样交给系统 shell 的命令（Linux 为 /bin/sh，Windows 为 cmd.exe）；
                                        必须阻塞到单元结束才返回（集群作业用 sbatch --wait、kubectl wait 之类的阻塞提交）
   "cwd": ".",                          可选，运行目录，默认当前目录
   "done_file": "out/u001/RESULT.json", 该文件存在即视为完成；恢复时据此跳过
   "requires": ["out/common/x.npz"],    可选，启动前必须存在的输入；由其他单元产出的会等它们完成
   "retries": 2}                        可选，覆盖 --retries
相对路径相对于运行本脚本时的当前目录。

status 的输出只有五种，每种一行，只看开头的大写词：
  NO_STATE                                             还没有运行过
  NOT_RUNNING done=A isolated=B pending=D              运行进程已退出但还有单元没做（冒烟之后、被 stop 或中断）
  RUNNING done=A isolated=B running=C pending=D        运行进程在工作，或在等待内存、等待重试
  RUNNER_STALE done=A isolated=B running=C pending=D   运行进程超过 --stale-after 秒没有心跳
  ALL_TERMINAL done=A isolated=B                       全部单元到达终态
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

TERMINAL = ("DONE", "ISOLATED")
TS = "%Y-%m-%d %H:%M:%S"


def now():
    return datetime.datetime.now().strftime(TS)


def progress(path, unit, status, detail=""):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{now()} | {unit} | {status} | {detail}\n")


def load_units(path, only=None):
    units, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            u = json.loads(raw)
            for k in ("id", "cmd", "done_file"):
                if k not in u:
                    sys.exit(f"units.jsonl 第 {n} 行缺少字段 {k}")
            if u["id"] in seen:
                sys.exit(f"units.jsonl 第 {n} 行 id 重复: {u['id']}")
            seen.add(u["id"])
            units.append(u)
    if only:
        wanted = set(only.split(","))
        units = [u for u in units if u["id"] in wanted]
        missing = wanted - {u["id"] for u in units}
        if missing:
            sys.exit("units.jsonl 中没有这些 id: " + ", ".join(sorted(missing)))
    return units


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"heartbeat": None, "runner_active": False, "units": {}}


def save_state(path, state):
    state["heartbeat"] = now()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def stop_flag(a):
    return a.state + ".stop"


def free_gb():
    """可用物理内存（GiB）。不支持的系统返回 None，调用方跳过检查。"""
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                    (n, ctypes.c_ulonglong)
                    for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                              "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]

            m = MS()
            m.dwLength = ctypes.sizeof(m)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.ullAvailPhys / 2 ** 30
            return None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 2 ** 20
    except Exception:
        return None
    return None


def tail(path, n=3):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4000))
            data = f.read().decode("utf-8", "replace")
        lines = [ln for ln in data.splitlines() if ln.strip() and not ln.startswith("=== attempt")]
        return " / ".join(lines[-n:])[:300]
    except Exception:
        return ""


def counts(su):
    c = {s: 0 for s in ("DONE", "ISOLATED", "RUNNING", "PENDING", "RETRY_WAIT")}
    for r in su.values():
        c[r["status"]] = c.get(r["status"], 0) + 1
    return c


def detach(a):
    argv = [sys.executable, os.path.abspath(__file__)] + [x for x in sys.argv[1:] if x != "--detach"]
    out = open(a.runner_log, "a", encoding="utf-8")
    kw = {"stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(argv, **kw)
    print(f"LAUNCHED pid={p.pid} log={a.runner_log}")
    return 0


def finish(a, state, su, running, stopped):
    for uid, (p, lf) in running.items():
        try:
            p.terminate()
        except Exception:
            pass
        lf.close()
    state["runner_active"] = False
    save_state(a.state, state)
    c = counts(su)
    pending = c["PENDING"] + c["RETRY_WAIT"] + len(running)
    if stopped == "interrupted":
        progress(a.progress, "runner", "INTERRUPTED", f"{len(running)} 个单元被中止；再次 run 会重新排队")
        return 130
    if stopped == "stop":
        progress(a.progress, "runner", "STOPPED", f"按 stop 请求退出 done={c['DONE']} isolated={c['ISOLATED']} pending={pending}")
        print(f"STOPPED done={c['DONE']} isolated={c['ISOLATED']} pending={pending}")
        return 0
    progress(a.progress, "runner", "ALL_TERMINAL", f"done={c['DONE']} isolated={c['ISOLATED']}")
    print(f"ALL_TERMINAL done={c['DONE']} isolated={c['ISOLATED']}")
    return 0


def cmd_run(a):
    if a.detach:
        return detach(a)
    units = load_units(a.units, a.only)
    state = load_state(a.state)
    su = state["units"]
    os.makedirs(a.log_dir, exist_ok=True)
    if os.path.exists(stop_flag(a)):
        os.remove(stop_flag(a))
    producers = {os.path.normpath(u["done_file"]): u["id"] for u in units}
    by_id = {u["id"]: u for u in units}

    for u in units:  # 恢复
        rec = su.setdefault(u["id"], {"status": "PENDING", "attempts": 0})
        if os.path.exists(u["done_file"]):
            if rec["status"] != "DONE":
                rec["status"] = "DONE"
                progress(a.progress, u["id"], "DONE", u["done_file"] + " (已存在，跳过)")
        elif rec["status"] in ("RUNNING", "RETRY_WAIT"):
            rec["status"] = "PENDING"
        elif rec["status"] == "ISOLATED" and a.retry_isolated:
            rec["status"], rec["attempts"] = "PENDING", 0
    state["runner_active"] = True
    save_state(a.state, state)

    running, retry_at, last_mem_note = {}, {}, 0.0
    c = counts(su)
    progress(a.progress, "runner", "STARTED",
             f"concurrency={a.concurrency} units={len(units)} done={c['DONE']} isolated={c['ISOLATED']} pending={c['PENDING']}")
    try:
        while True:
            for uid in list(running):  # 收割
                p, lf = running[uid]
                rc = p.poll()
                if rc is None:
                    continue
                lf.close()
                del running[uid]
                u, rec = by_id[uid], su[uid]
                if rc == 0 and os.path.exists(u["done_file"]):
                    rec["status"] = "DONE"
                    progress(a.progress, uid, "DONE", u["done_file"])
                else:
                    limit = u.get("retries", a.retries)
                    why = f"exit {rc}" if rc != 0 else f"exit 0 但产物未出现 {u['done_file']}"
                    detail = f"{why}: {tail(os.path.join(a.log_dir, uid + '.log'))}"
                    if rec["attempts"] <= limit:
                        rec["status"] = "RETRY_WAIT"
                        retry_at[uid] = time.time() + a.retry_sleep
                        progress(a.progress, uid, f"RETRY {rec['attempts']}/{limit}", detail)
                    else:
                        rec["status"], rec["last_error"] = "ISOLATED", detail
                        progress(a.progress, uid, "ISOLATED", f"{rec['attempts']} 次尝试后 {detail}")
                save_state(a.state, state)

            for uid, t in list(retry_at.items()):  # 重试到期
                if time.time() >= t:
                    su[uid]["status"] = "PENDING"
                    del retry_at[uid]

            stop_requested = os.path.exists(stop_flag(a))
            pending = [] if stop_requested else [u for u in units if su[u["id"]]["status"] == "PENDING"]
            launched, deferred, mem_blocked = 0, 0, False
            for u in pending:  # 发射
                if len(running) >= a.concurrency:
                    break
                missing = [r for r in u.get("requires", []) if not os.path.exists(r)]
                if missing:
                    prod = [producers.get(os.path.normpath(m)) for m in missing]
                    if all(pid and su[pid]["status"] not in TERMINAL for pid in prod):
                        deferred += 1
                        continue
                    su[u["id"]]["status"] = "ISOLATED"
                    su[u["id"]]["last_error"] = "缺少输入: " + ", ".join(missing)
                    progress(a.progress, u["id"], "ISOLATED", "缺少输入: " + ", ".join(missing))
                    save_state(a.state, state)
                    continue
                if a.min_free_gb > 0:
                    fg = free_gb()
                    if fg is not None and fg < a.min_free_gb:
                        mem_blocked = True
                        if time.time() - last_mem_note > 600:
                            progress(a.progress, "runner", "WAIT_MEMORY",
                                     f"可用 {fg:.1f} GB < {a.min_free_gb} GB，等待后再启动 {u['id']}")
                            last_mem_note = time.time()
                        break
                rec = su[u["id"]]
                rec["attempts"] += 1
                rec["status"] = "RUNNING"
                lf = open(os.path.join(a.log_dir, u["id"] + ".log"), "a", encoding="utf-8")
                lf.write(f"\n=== attempt {rec['attempts']} at {now()} ===\n{u['cmd']}\n")
                lf.flush()
                p = subprocess.Popen(u["cmd"], shell=True, cwd=u.get("cwd") or None, stdout=lf,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                running[u["id"]] = (p, lf)
                launched += 1
                progress(a.progress, u["id"], f"START {rec['attempts']}", u["cmd"][:200])
                save_state(a.state, state)

            if stop_requested and not running:
                return finish(a, state, su, running, "stop")
            still_pending = [u for u in units if su[u["id"]]["status"] == "PENDING"]
            if not running and not retry_at and not still_pending:
                return finish(a, state, su, running, None)
            if (not running and not retry_at and still_pending and not launched and not mem_blocked
                    and deferred == len(still_pending)):
                for u in still_pending:  # 只剩互相等待的单元：依赖环或上游未定义
                    su[u["id"]]["status"] = "ISOLATED"
                    su[u["id"]]["last_error"] = "上游未产出"
                    progress(a.progress, u["id"], "ISOLATED", "上游未产出: " + ", ".join(u.get("requires", [])))
                return finish(a, state, su, running, None)
            save_state(a.state, state)  # 心跳
            time.sleep(a.poll)
    except KeyboardInterrupt:
        return finish(a, state, su, running, "interrupted")


def status_line(a):
    if not os.path.exists(a.state):
        return "NO_STATE"
    st = load_state(a.state)
    su = dict(st["units"])
    if a.units:
        for u in load_units(a.units):
            if u["id"] not in su:
                su[u["id"]] = {"status": "DONE" if os.path.exists(u["done_file"]) else "PENDING", "attempts": 0}
    c = counts(su)
    pending = c["PENDING"] + c["RETRY_WAIT"]
    if c["RUNNING"] == 0 and pending == 0:
        return f"ALL_TERMINAL done={c['DONE']} isolated={c['ISOLATED']}"
    if not st.get("runner_active"):
        return f"NOT_RUNNING done={c['DONE']} isolated={c['ISOLATED']} pending={pending + c['RUNNING']}"
    stale = True
    if st.get("heartbeat"):
        age = (datetime.datetime.now() - datetime.datetime.strptime(st["heartbeat"], TS)).total_seconds()
        stale = age > a.stale_after
    head = "RUNNER_STALE" if stale else "RUNNING"
    return f"{head} done={c['DONE']} isolated={c['ISOLATED']} running={c['RUNNING']} pending={pending}"


def cmd_status(a):
    deadline = time.time() + max(0, a.wait)
    while True:
        line = status_line(a)
        if not line.startswith("RUNNING") or time.time() >= deadline:
            print(line)
            return 0
        time.sleep(min(30, max(1, deadline - time.time())))


def cmd_stop(a):
    with open(stop_flag(a), "w", encoding="utf-8") as f:
        f.write(now())
    print("STOP_REQUESTED")
    return 0


def cmd_list(a):
    units = load_units(a.units)
    su = load_state(a.state)["units"]
    for u in units:
        st = su.get(u["id"], {}).get("status", "PENDING")
        print(f"{u['id']}\t{st}\tdone_file={'yes' if os.path.exists(u['done_file']) else 'no'}\t{u['cmd'][:80]}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="机械运行单元清单")
    sp = ap.add_subparsers(dest="sub", required=True)

    def common(p):
        p.add_argument("--state", default="runner_state.json")

    r = sp.add_parser("run")
    common(r)
    r.add_argument("--units", required=True)
    r.add_argument("--only", default=None, help="只运行这些 id（逗号分隔），用于冒烟")
    r.add_argument("--concurrency", type=int, default=1)
    r.add_argument("--retries", type=int, default=2, help="每单元失败后的重试次数")
    r.add_argument("--retry-sleep", type=float, default=60, help="重试前等待秒数")
    r.add_argument("--min-free-gb", type=float, default=0, help="可用内存低于此值时等待，不启动新单元")
    r.add_argument("--poll", type=float, default=5)
    r.add_argument("--progress", default="PROGRESS.md")
    r.add_argument("--log-dir", default="logs")
    r.add_argument("--runner-log", default="runner.log")
    r.add_argument("--retry-isolated", action="store_true", help="把已隔离的单元重新排队")
    r.add_argument("--detach", action="store_true", help="后台运行，立即返回 LAUNCHED pid=...")
    r.set_defaults(fn=cmd_run)

    s = sp.add_parser("status")
    common(s)
    s.add_argument("--units", default=None, help="给出后，清单里尚未进入状态文件的单元按 PENDING 计")
    s.add_argument("--wait", type=float, default=0)
    s.add_argument("--stale-after", type=float, default=600)
    s.set_defaults(fn=cmd_status)

    t = sp.add_parser("stop")
    common(t)
    t.set_defaults(fn=cmd_stop)

    l = sp.add_parser("list")
    common(l)
    l.add_argument("--units", required=True)
    l.set_defaults(fn=cmd_list)

    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
