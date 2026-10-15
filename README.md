# write-experiment-manual

研究侧 agent 用来编写实验手册的 skill。目标：把手册交给任何执行侧 agent（包括弱模型），它都能把精力放在实验本身，并把实验做完。

手册是一台按键机器：每一步一条原样运行的命令、一个要看的输出、一张“输出 → 下一步”表。并发、重试、恢复、进度由随手册分发的 `run_units.py` 机械完成。执行侧不估计、不判断、不等授权、不自行探索环境；没有墙钟截止、审批节点、前后审计、全量哈希这类与实验无关的东西。

## 安装

Claude Code 与 Codex，用户级安装，任一项目都可用：

```bash
npx skills add HappyEpicDragon/write-experiment-manual -g -a claude-code -a codex -y
```

只装到当前项目去掉 `-g`。更新时再运行一次同一命令。手动安装：把本仓库整个目录放到 `~/.claude/skills/write-experiment-manual/` 或 `~/.codex/skills/write-experiment-manual/`。

## 用法

在研究侧会话里说“用 write-experiment-manual 写本批实验手册”即可，Codex 里是 `$write-experiment-manual`。产出一个手册目录：

| 文件 | 内容 |
|---|---|
| `README.md` | 执行约定（固定块）、本批做什么、环境与入口、运行机制、步骤表、交付清单 |
| `PROTOCOL.json` 或 `SPEC.md` | 矩阵、数值、数学定义、指标、随机流、算例，数值只在这里维护 |
| `units.jsonl` | 每行一个单元：id、cmd、done_file、requires |
| `run_units.py` | 从本仓库 `assets/` 原样复制 |
| `checks/` | 算例与核对脚本，各打印一行固定字符串 |
| `LAUNCH_PROMPT.md` | 粘贴给执行 agent 的第一条消息 |

把目录交给实验侧，再把 `LAUNCH_PROMPT.md` 的内容粘贴给执行 agent。执行侧从步骤表第 1 步开始，按表走到标为“结束”的那一步。

## 仓库结构

| 文件 | 作用 |
|---|---|
| `SKILL.md` | 编写规则 |
| `references/executor-preamble.md` | 原样放进每份手册的“执行约定”固定块与启动提示词模板 |
| `references/manual-shape.md` | 手册目录与入口文件六节的骨架 |
| `references/runner.md` | 运行脚本的字段、命令、行为与五种状态的去向 |
| `references/branch-examples.md` | 含糊写法与按键写法的对照 |
| `assets/run_units.py` | 随手册分发的运行脚本，仅标准库，Python 3.8 以上 |
| `tests/test_runner.py` | 运行脚本的行为测试：`python -I tests/test_runner.py assets/run_units.py` |
| `agents/openai.yaml` | Codex 的界面元数据 |

## 运行脚本一览

```bash
python run_units.py run --units units.jsonl --only <首个单元 id> --retries 0        # 冒烟，前台
python run_units.py run --units units.jsonl --concurrency 4 --retries 2 --retry-sleep 60 --min-free-gb 8 --detach   # 启动，后台
python run_units.py status --units units.jsonl --wait 540                           # 等待，阻塞至多 540 秒
python run_units.py stop                                                            # 不再启动新单元
```

`status` 只输出五种开头：`NO_STATE`、`NOT_RUNNING`、`RUNNING`、`RUNNER_STALE`、`ALL_TERMINAL`，每种在步骤表里有固定去向。单元失败按固定次数重试后隔离，整批不停；内存不足时等待而不是停止；重新运行按产物文件跳过已完成单元。
