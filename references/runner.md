# 运行脚本说明

`assets/run_units.py` 随本技能分发，复制到手册目录后不改动。它把调度、并发、重试、恢复、进度全部机械化：执行者只运行步骤表里给的几条命令，看它打印的固定字符串。

## 什么时候用

仓库已有的运行入口同时满足四条就直接用，不必带本脚本：一条命令启动整个矩阵或阶段；重新运行按产物文件跳过已完成单元；单个单元失败只重试固定次数后隔离，整批不停；进度写入文件。缺任何一条，就用本脚本包一层：把仓库入口变成 units.jsonl 里每个单元的 cmd。

## 单元清单 units.jsonl

每行一个 JSON 对象：

| 字段 | 必填 | 含义 |
|---|---|---|
| id | 是 | 唯一标识，由矩阵坐标拼成，如 `s158_e090_W2` |
| cmd | 是 | 原样交给系统 shell 的命令（Linux 为 /bin/sh，Windows 为 cmd.exe），必须阻塞到单元结束才返回；集群用 `sbatch --wait ...`、`kubectl wait --for=condition=complete --timeout=-1 ...` 之类 |
| done_file | 是 | 单元最后写出的文件；存在即视为完成，恢复时据此跳过 |
| cwd | 否 | 运行目录，默认当前目录 |
| requires | 否 | 启动前必须存在的输入文件；由其他单元产出的会等它们完成，上游被隔离则本单元也隔离 |
| retries | 否 | 覆盖命令行的 --retries |

相对路径相对于运行脚本时的当前目录，即「环境与入口」的工作目录。矩阵大时由 `make_units.py` 从规格展开生成，脚本只做展开，不做调度、不做检查。

## 命令

| 用途 | 命令 |
|---|---|
| 冒烟，前台只跑一个单元 | `python run_units.py run --units units.jsonl --only s158_e090_W2 --retries 0` |
| 启动，后台跑整个清单 | `python run_units.py run --units units.jsonl --concurrency 4 --retries 2 --retry-sleep 60 --min-free-gb 8 --detach` |
| 状态，最多阻塞 540 秒 | `python run_units.py status --units units.jsonl --wait 540` |
| 请求收口 | `python run_units.py stop` |
| 清单 | `python run_units.py list --units units.jsonl` |

参数全部是数字或路径，由编写者写死在步骤表里，执行者不改：

| 参数 | 含义 | 编写者怎么定 |
|---|---|---|
| --concurrency | 同时运行的单元数 | 上批记录；或机器容量除以单元峰值内存；没有记录写 1 |
| --retries | 每单元失败后的重试次数 | 本机 2；抢占频繁的集群分区可以写大 |
| --retry-sleep | 重试前等待秒数 | 本机 60；集群排队可写 300 |
| --min-free-gb | 可用内存低于此值时等待，不启动新单元 | 本机单元按单元峰值加系统余量写；集群单元写 0 |
| --wait | status 最多阻塞秒数 | 小于执行侧工具的单次命令超时，常用 540 |
| --stale-after | 多久没有心跳算运行进程失联 | 默认 600 |
| --only | 只跑这些 id | 冒烟时填第一个单元 |

## 行为

- 启动时产物已存在的单元标 DONE 跳过；上次中断时还在运行的单元重新排队，尝试次数保留。
- 命令退出码非 0，或退出码 0 但产物未出现，都算失败；失败后等 --retry-sleep 秒重试，次数用完标 ISOLATED 并记录日志末尾几行。整批不因任何单元失败而停止。
- requires 缺失且没有单元会产出它，立即 ISOLATED；上游还在运行或等待重试就等待。
- --min-free-gb 大于 0 时，启动每个单元前读可用物理内存，不足就等待，每 10 分钟写一行 WAIT_MEMORY；不会因此停止或跳过。
- 每个单元的输出在 `logs/<id>.log`，每次尝试一段；进度在 PROGRESS.md，每行“时间 | 单元 | 状态 | 说明”；状态在 `runner_state.json`。
- `stop` 只让运行进程不再启动新单元，运行中的单元跑完后退出；下次 `run` 自动继续剩余单元。
- 脚本没有任何时间限制。

## status 的输出与去向

| 输出 | 含义 | 步骤表里的去向 |
|---|---|---|
| `NO_STATE` | 从未运行过 | 恢复步里到冒烟步；等待步里到启动步 |
| `NOT_RUNNING done=A isolated=B pending=D` | 运行进程已退出但还有单元没做：冒烟之后、被 stop 或中断 | 启动步 |
| `RUNNING done=A isolated=B running=C pending=D` | 运行进程在工作，或在等待内存、等待重试 | 等待步本身 |
| `RUNNER_STALE done=A isolated=B running=C pending=D` | 运行进程超过 --stale-after 秒没有心跳 | 启动步，脚本按产物恢复 |
| `ALL_TERMINAL done=A isolated=B` | 全部单元到达终态 | 汇总步 |

只看开头的大写词，数字只用于记录。status 一律带 `--units units.jsonl`，否则清单里尚未运行过的单元不计入。
