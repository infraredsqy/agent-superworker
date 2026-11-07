# agent-superworker

[English](README.md) | 中文

`agent-superworker`（`sw`）由一条命令与一条执行管线构成：前者维护项目的待办依赖图，后者按节点将工作逐个交付给一个 agent 会话。

想法以节点形式记录在一张有向无环图（DAG）中：节点代表一项待办，边代表依赖关系。一条 **cycle**（执行线）选取当前优先级最高的节点，交由一个新开的会话处理，并记录其返回的结果。会话在 `sw-conduct` 技能下完成准备、执行、独立复审、修复与收尾各阶段，最后提交一行结论，说明是否完成交付。

管线所掌握的全部状态均存放于 `.superworker/` 目录。

## 项目状态

本项目仍处于快速迭代期，可能包含不兼容的变更，文件格式亦可能随之调整。

`sw next --spawn` 与 `sw dispatch` 会启动真实的 agent 会话，并产生实际的 token 消耗。

## 安装

环境要求：Python 3.10 及以上、git，以及 Windows 平台上的 PowerShell。在本仓库内执行：

```pwsh
git clone https://github.com/infraredsqy/agent-pathfinder/
cd agent-superworker
pwsh -File install.ps1 -SkillsFor none    # 不建技能链接；-SkillsFor zcode 才链进 ~/.agents/skills
```

脚本没有缺省宿主，因为技能根归某一家：`-SkillsFor zcode` 把 `sw-*` 技能链进 `~/.agents/skills`（ZCode 读的那一处），`-SkillsFor none` 只把 `tools/bin` 加入 `PATH`。`-SkillsFor dsh` 是有意拒绝的——DSH 那边由插件把 DSH 形状的技能装进 `~/.dsh/skills`。两种装法都会摘掉其它 checkout 的 `PATH` 条目，所以 `sw` 只解析到本仓库；仓库位置变更后重新执行即可，脚本具备幂等性，收尾自检里包含一次 `sw doctor`。`-Check` 把将要建的链接、`PATH` 与记录逐条打印出来，一个字节都不写。

技能分英文与中文两套，一次只装一套（一个方法名只有一份技能）：缺省装英文，`-Lang zh` 装中文那一套。选择记在 `~/.superworker/lang.txt`，之后重跑修复不会把语言换掉。

```pwsh
sw version     # 当前 checkout 的位置
sw doctor      # 检查本仓库与当前项目的状态
```

## 使用

在目标项目的根目录下执行：

```pwsh
sw init                                        # 创建 .superworker/，并登记合并驱动
sw dag add "billing page" --tags coding
sw dag add "rate-limit the API" --depends-on <uuid>
sw dag list                                    # 按 urgency × importance 排序
```

随后创建一条执行线，并由其启动首个会话：

```pwsh
sw cycle start billing --title "billing" --tags coding
sw next --spawn
```

`sw next` 是唯一入口：直接执行将推进至下一个动作；`--plan` 给出同样的结果，但不写入任何内容。

## 技能

执行会话由以下技能驱动，均链接至 `~/.agents/skills`：

| 技能 | 职责 |
|---|---|
| `sw-route` | 将需求梳理为节点；维护与查询依赖图 |
| `sw-conduct` | 完成单个节点：准备、执行、独立复审、修复、收尾 |
| `sw-instruct` | 记录一条约束，供后续会话遵循 |

插件还自带四张技能：`sw-issue-scan`、`sw-issue-discuss`、`sw-issue-repair`、`sw-test-audit`。插件的技能放在该插件自己的文件夹里（`plugins/<name>/skills/`），随插件一起装、卸，所以本仓库装了哪些，以 `sw doctor` 列出来的为准。别的插件带的技能不点名——要提它，说「跑 `sw doctor`」。

## 与 DSH 的关系

同一套记录层另有一份实现在 DeepSeek Harness 里，就是 `dsh-superworker` 插件：同一个 `.superworker/` 布局、同一套记录字节规则、同一个合并驱动，所以一个项目的记录两家都能读、都能写。词面不共用——DSH 有它自己的一套，本命令只有 `sw --help` 印出来的那套——DSH 有而这里没有的词，等这里的技能需要它时再补。技能树也是分开的：插件把 DSH 那套写进 `~/.dsh/skills`，本仓库的 `skills/` 放的是命令行这一套。

## 开发

各工具的套件都在 `tools/` 下跑（在 `tools/` 根上 discover 一个用例都跑不到）：

```pwsh
cd tools
python -m unittest discover -s dag/tests -t .
python -m unittest discover -s shared/tests -t .
python -m unittest discover -s conduct/tests -t .
```

插件自己的套件是插件自己的：在仓库根上跑，top level 指向插件的 `tools/`——插件是一个文件夹，它的测试跟着它走。

```pwsh
python -m unittest discover -s plugins/issue/tools/tests -t plugins/issue/tools
```

契约文档位于 `docs/`（中文）。

## 许可证

本项目采用 [MIT](LICENSE) 许可证。