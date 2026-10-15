# -*- coding: utf-8 -*-
"""run_units.py 的行为测试。

用法（在仓库根目录）: python -I tests/test_runner.py assets/run_units.py
测试目录建在系统临时目录下，不写入仓库。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

RUNNER_SRC = os.path.abspath(sys.argv[1])
BASE_DIR = tempfile.mkdtemp(prefix="run_units_test_")
PY = sys.executable
results = []


def check(name, cond, info=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  <- {info}"))


def fresh(name):
    d = os.path.join(BASE_DIR, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "out"))
    shutil.copy(RUNNER_SRC, os.path.join(d, "run_units.py"))
    return d


def run(d, *args, timeout=120):
    p = subprocess.run([PY, "run_units.py", *args], cwd=d, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def write_units(d, units):
    with open(os.path.join(d, "units.jsonl"), "w", encoding="utf-8") as f:
        for u in units:
            f.write(json.dumps(u, ensure_ascii=False) + "\n")


def read(d, name):
    with open(os.path.join(d, name), encoding="utf-8") as f:
        return f.read()


def pyc(code):
    return f'{PY} -c "{code}"'


BASE = [
    {"id": "u1", "cmd": pyc("open('out/u1.txt','w').write('1')"), "done_file": "out/u1.txt"},
    {"id": "u2", "cmd": pyc("import sys;print('boom');sys.exit(1)"), "done_file": "out/u2.txt", "retries": 1},
    {"id": "u3", "cmd": pyc("open('out/u3.txt','w').write('3')"), "done_file": "out/u3.txt", "requires": ["out/u1.txt"]},
    {"id": "u4", "cmd": pyc("open('out/u4.txt','w').write('4')"), "done_file": "out/u4.txt", "requires": ["out/nobody.txt"]},
    {"id": "u5", "cmd": pyc("open('out/u5.txt','w').write('5')"), "done_file": "out/u5.txt", "requires": ["out/u2.txt"]},
]

# 1. 完整一轮：成功、失败重试后隔离、依赖等待、缺输入隔离、上游隔离
d = fresh("t1")
write_units(d, BASE)
rc, out, err = run(d, "run", "--units", "units.jsonl", "--concurrency", "2", "--retries", "2",
                   "--retry-sleep", "1", "--poll", "0.3")
check("t1 exit 0", rc == 0, err)
check("t1 ALL_TERMINAL done=2 isolated=3", out.endswith("ALL_TERMINAL done=2 isolated=3"), out)
prog = read(d, "PROGRESS.md")
st = json.load(open(os.path.join(d, "runner_state.json"), encoding="utf-8"))
check("t1 u2 retried once then isolated (retries=1 override)", "u2 | RETRY 1/1" in prog and "u2 | ISOLATED" in prog
      and st["units"]["u2"]["attempts"] == 2, prog)
check("t1 u3 waited for u1 and finished", st["units"]["u3"]["status"] == "DONE")
check("t1 u4 isolated for missing input", "u4 | ISOLATED | 缺少输入" in prog, prog)
check("t1 u5 isolated after upstream isolated", st["units"]["u5"]["status"] == "ISOLATED", prog)
check("t1 log has boom", "boom" in read(d, os.path.join("logs", "u2.log")))
check("t1 runner_active false at end", st["runner_active"] is False)

# 2. 重新运行：已完成跳过，不重做
rc, out, err = run(d, "run", "--units", "units.jsonl", "--retries", "0", "--retry-sleep", "1", "--poll", "0.3")
st2 = json.load(open(os.path.join(d, "runner_state.json"), encoding="utf-8"))
check("t2 resume ALL_TERMINAL done=2 isolated=3", out.endswith("ALL_TERMINAL done=2 isolated=3"), out)
check("t2 u1 not re-run", st2["units"]["u1"]["attempts"] == 1)
rc, out, err = run(d, "status", "--units", "units.jsonl")
check("t2 status ALL_TERMINAL", out == "ALL_TERMINAL done=2 isolated=3", out)
rc, out, err = run(d, "list", "--units", "units.jsonl")
check("t2 list shows 5 lines", len(out.splitlines()) == 5, out)

# 3. --only 冒烟，然后 status --units 报 NOT_RUNNING
d = fresh("t3")
write_units(d, BASE)
rc, out, err = run(d, "run", "--units", "units.jsonl", "--only", "u1", "--retries", "0", "--poll", "0.3")
check("t3 smoke ALL_TERMINAL done=1 isolated=0", out.endswith("ALL_TERMINAL done=1 isolated=0"), out)
rc, out, err = run(d, "status", "--units", "units.jsonl")
check("t3 status NOT_RUNNING pending=4", out == "NOT_RUNNING done=1 isolated=0 pending=4", out)
rc, out, err = run(d, "status")
check("t3 status without --units says ALL_TERMINAL for known units only", out == "ALL_TERMINAL done=1 isolated=0", out)

# 4. --detach + status --wait
d = fresh("t4")
write_units(d, [{"id": "slow", "cmd": pyc("import time;time.sleep(5);open('out/slow.txt','w').write('x')"),
                 "done_file": "out/slow.txt"}])
rc, out, err = run(d, "run", "--units", "units.jsonl", "--poll", "0.3", "--detach")
check("t4 detach prints LAUNCHED", out.startswith("LAUNCHED pid="), out + err)
time.sleep(1.5)
rc, out, err = run(d, "status", "--units", "units.jsonl", "--wait", "1")
check("t4 status RUNNING while slow unit runs", out.startswith("RUNNING ") and "running=1" in out, out)
rc, out, err = run(d, "status", "--units", "units.jsonl", "--wait", "30", timeout=60)
check("t4 status --wait returns ALL_TERMINAL", out == "ALL_TERMINAL done=1 isolated=0", out)
check("t4 runner.log exists", os.path.exists(os.path.join(d, "runner.log")))

# 5. 内存阈值等待 + stop + 重新启动
d = fresh("t5")
write_units(d, [{"id": "m1", "cmd": pyc("open('out/m1.txt','w').write('1')"), "done_file": "out/m1.txt"}])
rc, out, err = run(d, "run", "--units", "units.jsonl", "--min-free-gb", "100000", "--poll", "0.3", "--detach")
time.sleep(2)
rc, out, err = run(d, "status", "--units", "units.jsonl")
check("t5 RUNNING while waiting for memory", out.startswith("RUNNING ") and "pending=1" in out, out)
check("t5 WAIT_MEMORY logged", "WAIT_MEMORY" in read(d, "PROGRESS.md"), read(d, "PROGRESS.md"))
rc, out, err = run(d, "stop")
check("t5 stop prints STOP_REQUESTED", out == "STOP_REQUESTED", out)
rc, out, err = run(d, "status", "--units", "units.jsonl", "--wait", "15", timeout=40)
check("t5 after stop NOT_RUNNING pending=1", out == "NOT_RUNNING done=0 isolated=0 pending=1", out)
check("t5 STOPPED logged", "STOPPED" in read(d, "PROGRESS.md"))
rc, out, err = run(d, "run", "--units", "units.jsonl", "--poll", "0.3")
check("t5 relaunch completes", out.endswith("ALL_TERMINAL done=1 isolated=0"), out)
check("t5 stop flag cleared", not os.path.exists(os.path.join(d, "runner_state.json.stop")))

# 6. RUNNER_STALE：心跳过期且 runner_active
d = fresh("t6")
write_units(d, BASE[:1])
st = {"heartbeat": "2020-01-01 00:00:00", "runner_active": True, "units": {"u1": {"status": "RUNNING", "attempts": 1}}}
json.dump(st, open(os.path.join(d, "runner_state.json"), "w", encoding="utf-8"))
rc, out, err = run(d, "status", "--units", "units.jsonl")
check("t6 RUNNER_STALE", out.startswith("RUNNER_STALE "), out)
rc, out, err = run(d, "run", "--units", "units.jsonl", "--poll", "0.3")
check("t6 stale RUNNING unit re-queued and completes", out.endswith("ALL_TERMINAL done=1 isolated=0"), out)
d7 = fresh("t7")
rc, out, err = run(d7, "status")
check("t7 NO_STATE", out == "NO_STATE", out)

# 7. --retry-isolated
d = fresh("t8")
write_units(d, BASE[:2])
run(d, "run", "--units", "units.jsonl", "--retries", "0", "--retry-sleep", "1", "--poll", "0.3")
st0 = json.load(open(os.path.join(d, "runner_state.json"), encoding="utf-8"))
check("t8 first pass: u2 unit-level retries=1 gives 2 attempts", st0["units"]["u2"]["attempts"] == 2, str(st0))
rc, out, err = run(d, "run", "--units", "units.jsonl", "--retries", "0", "--retry-sleep", "1", "--poll", "0.3", "--retry-isolated")
st = json.load(open(os.path.join(d, "runner_state.json"), encoding="utf-8"))
check("t8 retry-isolated resets and re-attempts u2 (2 attempts again, still isolated)",
      st["units"]["u2"]["attempts"] == 2 and st["units"]["u2"]["status"] == "ISOLATED" and out.endswith("ALL_TERMINAL done=1 isolated=1"), out)

n_fail = sum(1 for _, ok in results if not ok)
print(f"\n{len(results) - n_fail}/{len(results)} checks passed  (temp dir: {BASE_DIR})")
shutil.rmtree(BASE_DIR, ignore_errors=True)
sys.exit(1 if n_fail else 0)
