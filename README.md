# 本地文档台（docdesk）

> 一个用完即焚的本地文档问答台：拖进 PDF/扫描件，用本地大模型问它，关掉就什么都不剩。
> A throwaway, offline document-Q&A desk — everything lives in RAM and dies with the process.

在一块 8 GB 显存的消费级显卡上，用来自查私人文书的小工具：**拖文件进去、对话式提问、用完销毁**。
不联网、不落盘、不留日志；关机之后什么都没了。

```
浏览器（局域网或本机）
      │  上传 / SSE 流式回答
      ▼
app.py  ← 只在内存里：文档文本 + BM25 索引 + 对话历史
      │  OpenAI 兼容 /v1
      ▼
llama-server（只监听 127.0.0.1:8080，输出丢弃）
```

## 依赖

- Python 3.10+（只用标准库跑服务；解析库首次运行自动装进 `.venv`）
- 一个带 CUDA 的 `llama-server`（llama.cpp），能跑 7–9B 的 Q4 模型
- 可选：`tesseract` + `chi_sim` 语言包，用于扫描件

## 快速开始

```bash
cd ~/docdesk
./docdesk.sh                 # 只监听 127.0.0.1:8081，SSH 进去再用 ssh -L 转发
./docdesk.sh --lan           # 监听 0.0.0.0，打印带一次性令牌的局域网地址
./docdesk.sh --lan --model small --idle 15
./docdesk.sh stop            # 从别的终端停掉
```

启动约 60–75 秒（加载 5.8 GB 模型）。终端会打印地址，形如：

```
http://192.168.1.23:8081/?t=Xk3...      ← --lan 模式，令牌只在这个进程里
```

浏览器打开即可；`Ctrl-C` 或页面右上角「销毁并退出」结束全部进程。

## 能做什么

| 场景 | 怎么用 |
|---|---|
| 合同/协议逐条核对 | 上传后点「风险/义务」，或直接问"违约责任在哪几条" |
| 病历、体检报告、化验单 | 「数字与期限」抽成表格；追问"这个指标偏高意味着什么（只看资料）" |
| 银行流水、保险条款、发票 | 「数字与期限」；Excel 直接拖 |
| 会议纪要、访谈记录 | 「要点清单」「待办事项」 |
| 长论文/说明书 | 超过 4.2 万 token 自动切成检索模式；「全文摘要」走分段归纳再汇总 |
| 临时一段文字 | 点「粘贴文本」，不落盘 |

答案里的 `[3]` 是资料块编号，点一下就跳到那条原文（带页码/段落位置）。
模型被要求：数字、日期、金额、条款号照抄原文；资料里没有就说"资料中未提及"。

## 支持格式

PDF（有文字层的）、docx、xlsx、txt/md/csv/json/yaml、html、图片（png/jpg/webp/tiff）。
扫描版 PDF 会自动走 OCR，识别出来的页在引用里标成「第 3 页（OCR）」。

## 扫描件与 OCR

走的是 **tesseract 5.5（CPU，不占显存）**，300 dpi 渲染后 `chi_sim+eng` 识别：

| 测试 | 速度 | 字符错误率 |
|---|---|---|
| 英文论文页（300 dpi，与 PDF 文字层对比） | 2–4 s/页 | 1.2% |
| 中文页（Noto Sans SC，正文+表格+金额） | 1–3 s/页 | ≈0 |
| 中文页（文泉驿正黑，小字号） | 1.5–3 s/页 | 3.6% |

> 速度看 CPU 频率状态：`powersave` governor 下 3–4 s/页，负载上来后能到 1–2 s/页。

**能信**：整段意思、金额、日期、百分比、编号。
**别信**：形近字（期末→期未、交付→节付）、人名、小字号表格里的列对应关系。
所以 OCR 页只适合"看懂大意 + 找位置"，要逐字引用请对照原件。

安装：

```bash
sudo pacman -S tesseract tesseract-data-chi_sim tesseract-data-eng   # Arch
sudo apt install tesseract-ocr tesseract-ocr-chi-sim                 # Debian/Ubuntu
```

代码优先用系统安装（`/usr/share/tessdata` 里有 `chi_sim`），找不到才回退到
`DOCDESK_TESS` 指向的目录。没装也能启动，只是扫描件会提示识别不了。

## 模型

模型清单放在 `models.conf`（已在 `.gitignore` 里，不会被提交），格式一行一个：

```bash
main_path=/models/xxx-Q4_K_M.gguf
small_path=/models/yyy-IQ4_XS.gguf
```

首次使用：

```bash
cp models.conf.example models.conf   # 然后改成你自己的路径
./docdesk.sh --model main            # 用名字
./docdesk.sh --model /models/x.gguf  # 或者直接给路径
```

任何 7–9B 的 instruct 模型都能用（Qwen 系模板）。上下文默认 98304，`--ctx 65536` 可降；
IQ4_XS 这类更大的权重建议 64k，96k 容易 OOM。

## 为什么"关机就没了"

| 数据 | 在哪 | 怎么消失 |
|---|---|---|
| 上传的文件、抽出的文字、BM25 索引、对话历史 | Python 进程堆内存 | 进程退出即归还内核；关机即断电清空 |
| 界面里的聊天记录 | 浏览器内存（无 localStorage / 无 IndexedDB / 无 Service Worker） | 刷新页面即丢，关标签页即丢 |
| llama-server 的日志 | `/dev/null` | 从未写过 |
| SSH/终端 scrollback | 只有 URL 和"就绪"，没有文档内容 | 关终端 |
| 崩溃转储 | 启动脚本里 `ulimit -c 0` | 不会生成 |
| 临时文件 | 没有任何临时文件；`/tmp` 和 `/dev/shm` 本身也是 tmpfs | — |
| swap | 建议用 zram 或加密 swap；磁盘 swap 可能留下页副本 | 关机清空 |

刻意没有做的事：不缓存到磁盘、不写 sqlite、不导出 PDF、不做"历史会话"。

## 已实测的隐私检查

用一个带唯一标记的文档走完「上传 → 提问（模型把标记念出来）→ 页面销毁」，然后全盘搜：

```bash
grep -aRl "标记串" /tmp /dev/shm /var/tmp /var/log "$HOME" /root \
     /var/lib/systemd/coredump 2>/dev/null
journalctl --since "-40 min" | grep -c "标记串"
```

2026-10-27 实测：**文件 0 命中，journal 0 命中**，退出后显存回到 9 MiB、8080/8081 端口关闭。

### 顺手也清一下别处

这个工具自己不落盘，但**别的程序**可能替你记了东西：

```bash
sudo rm -f /var/lib/systemd/coredump/core.llama-server.*   # 以前的崩溃转储里可能有旧 prompt
sudo journalctl --vacuum-time=3d                           # 系统日志
```

如果之前用别的聊天前端跑过同一批文档，记得清那边的历史记录。

## 安全边界（公开部署前必读）

- 默认只监听 `127.0.0.1:8081`，用 `ssh -L` 转发访问，最稳。
- `--lan` 会监听 `0.0.0.0`，URL 里带一次性令牌；令牌每次启动都变，但走的是**明文 HTTP**，
  同一局域网内能抓包的人看得到。**不要**把它映射到公网，也不要在公共 Wi-Fi 上开 `--lan`。
- 模型服务固定监听 `127.0.0.1:8080`，没有鉴权，靠"只绑本机"保护。
- 单文件上限 120 MB；文本只进内存，不写临时文件。
- 没有 CSRF 防护、没有用户系统——它就是个给自己用的单用户工具。

## 能力边界（9B 模型的真实水平）

- **能**：20 万字以内的中文材料做检索问答、结构化摘要、按条款/日期抽取、翻译。
- **弱**：跨几十页做精确算术汇总（金额求和、表格合计）——请让它"列出来"，自己算。
- **弱**：扫描件、手写、图表里的数据、复杂跨页表格。
- **弱**：长文档逐页比对（"第 3 页和第 40 页矛盾吗"），检索会漏；这种请分两次传。
- 生成速度约 55 tok/s，一份 30 页 PDF 的全文问答 10–30 秒。

## 目录

```
docdesk.sh     启动器：起 llama-server + 起界面 + 退出时一起杀
models.conf    本机模型路径（不提交；模板是 models.conf.example）
pack.sh        打包成 tar.gz（不含 .venv）
app.py         界面服务 + 检索 + 模型代理（纯标准库）
extract.py     PDF/docx/xlsx/txt/图片 → 页块（pypdfium2、python-docx、openpyxl、tesseract）
retrieve.py    切块 + 中文 bigram BM25 + 引用编号
ui.html        单页界面（无外部资源）
.venv/         上面几个库，仅解析文档用（首次运行自动重建）
```

## 打包 / 搬家

```bash
./pack.sh                      # -> ~/docdesk-20261027.tar.gz，25 KB
./pack.sh /tmp/backup.tar.gz   # 或者指定位置
```

包里只有代码（25 KB），**不含 .venv 和模型**。换台机器解包后直接 `./docdesk.sh`：

- `.venv` 不存在 → 自动 `python3 -m venv` + 装 4 个解析库（需要联网，约 40 MB）
- `llama-server` 找不到 → 依次找 `$DOCDESK_LLAMA`、`PATH`、`~/src/llama.cpp-*/build/bin/`
- 模型目录默认 `/models`，可用 `DOCDESK_MODEL_DIR=/path/to/models` 覆盖

## 改界面之后

界面是单文件 `ui.html`，没有构建步骤。改完跑一下冒烟测试（假 DOM + quickjs，不需要浏览器）：

```bash
pip install quickjs
python3 tests/ui_smoke.py
```

它会真的执行上传、粘贴两条路径，能抓到 TDZ、拼错函数名这类运行时错误。

## 排错

- `端口 8081 被占用` → 可能已有一个在跑，`./docdesk.sh stop`
- `8080 上已有 llama-server` → 先停掉它（本工具要自己管模型进程，才能保证退出时释放显存）
- 模型加载失败 → `./docdesk.sh --debug`，日志在 `/tmp/docdesk-llama-*.log`（tmpfs，重启即消失）
- 显存不够 → `--ctx 65536`
- 局域网打不开 → 确认 `--lan`，且地址里带 `?t=` 令牌；令牌每次启动都变
