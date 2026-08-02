# 交接说明

本文档面向接手 `rev` 与 `feat/windows-support` 两条分支的下一位开发者。两者都基于上游 `0ed85d8`，全部为增量提交，未改写任何历史。

`feat/windows-support` 从 `rev` 的 `142d1d7` 开出，只含让 ChatApp 本体跑在 Windows 上所需的改动。它尚未合回 `rev`，且有三项行为**未经真机验证**（见第三节第 7 项）。如果你在 Linux 或 macOS 上工作，`rev` 就是你要的分支，这条可以先不管。

配套文档：`docs/UI_CONVENTIONS.md`（前端样式与图标约定，含改动时必须避开的陷阱清单）。

---

## 一、必须先做的两件事

### 1. 吊销已泄露的 API 密钥（用户批注：已读。现可无视）

`providers.json` 与 `settings.json` 曾被纳入版本控制，其中的凭据**已经进入 git 历史**：

- `providers.json` — 7 个供应商条目的 `api_key`，共 8 个密钥
- `settings.json` — `ANTHROPIC_AUTH_TOKEN`

本分支已将两个文件取消跟踪并加入 `.gitignore`，同时修掉了 `app.py` 中 `export_snapshot` 把真实 `settings.json` 打进更新包上传到公开仓库的第二条泄露通道。但**历史提交里的旧值仍然可以被 `git show` 检出**。

彻底清除需要 `git filter-repo` 改写历史并强制推送，属不可逆且影响共享状态的操作，因此没有做。正确的处理是到各供应商后台吊销重发——吊销之后历史里的字符串就是废纸，比清洗历史更彻底也更安全。

### 2. 首次运行前复制配置模板

真实配置文件已不在版本控制中，克隆后需要手动生成：

```bash
cp providers.example.json providers.json
cp settings.example.json settings.json
```

然后填入自己的 `api_key` 与 `ANTHROPIC_AUTH_TOKEN`。两份模板保留了完整的字段结构与默认值，凭据位置写成 `sk-REPLACE_WITH_YOUR_KEY` 占位符。

**不做这一步的症状**：服务能启动，但模型下拉列表为空，且不会有任何报错——`app.py` 的 `get_providers` 对 `FileNotFoundError` 返回空数组而非抛错。

### 3. 提示词不要复制

这一条与上面两条正好相反，所以单列出来免得被顺手照做。`prompts/` 下随仓库分发三份默认值（系统提示词与两档深度思考提示词），开箱即用，**不需要也不应该复制到 `data/`**。

想让自己的修改进版本控制就直接改 `prompts/`；只想在本机生效就把同名文件放进 `data/`，它会覆写默认值，不需要重启。两处都缺时该提示词为空并在控制台打印一次警告。

**为什么强调不要复制**：一旦 `data/` 下有了同名文件，解析器的存在性检查从此永远命中它，`prompts/` 里的任何后续改动（包括 `git pull` 带来的）对这台机器就彻底不可见了。旧实现正是在文件缺失时自动写一份到 `data/`，而 `CONFIG["SYSTEM_PROMPT"]` 是硬编码空串——于是写出的是一个空文件，模型在毫无协议约束的情况下工作而控制台一句话都没有。

---

## 二、改动范围

按主题归纳。逐提交的细节见 `git log`，那里是权威来源，此处不复述。

### 敏感配置分离

凭据文件移出版本控制，改为提供 `.example` 模板；补充忽略 `webfetch_cache/`；修复更新包打包时的凭据泄露。

### 提示词默认值随仓库分发

三份提示词的默认值移入仓库根 `prompts/` 并纳入追踪，`data/` 下的同名文件覆写它。上手说明见第一节第 3 项。

**`.gitignore` 刻意没动。** `data/` 是整目录排除，而 git 在父目录被排除后不再下降进去比对，所以在其下写 `!data/system_prompt.txt` 这类取反规则**必然静默失效**——这一条用隔离仓库实测过。放宽成 `data/*` 加逐项取反技术上可行，但那会让保护用户对话记录的规则从此依赖于每个人都理解取反的作用范围。放默认值到仓库根则完全不进入这个话题。

**顺带修掉一条数据丢失通道。** `app.py` 两处打包清单原先含 `data/` 下的提示词，而更新包里每一项都会被自动生成的 `update.py` 按相同相对路径 `shutil.copy2` 覆写——一次自动更新就静默覆盖用户改过的提示词。清单改为只发 `prompts/`，变量名一并从 `_release_data` / `_data_files` 改为 `_release_prompts` / `_prompt_files`，因为原名现在会主动误导。

### Windows 支持（`feat/windows-support` 分支）

新增 `api/platform_shell.py` 作为**唯一知道 `os.name` 的模块**。其余模块调用它的函数，因此新增平台、调整解释器回退链或改动编码对齐都是单点改动。

设计上有一条不能动的取向：**这些函数一律返回数据（argv 列表、Popen 关键字字典）而不自己起进程。** 理由不是风格——开发机是 Linux，如果 Windows 分支只存在于 `Popen` 调用内部，它在那台机器上就是永远无法被执行的死区，而它恰好是最容易写错的部分。返回数据意味着两个平台的分支都能在任意平台上被断言，`tests/test_platform_shell.py` 的 40 条用例正是靠这一点成立的。

三个 Win32 常量（`CREATE_NEW_PROCESS_GROUP`、`CREATE_NO_WINDOW`、`CTRL_BREAK_EVENT`）写成字面量而非 `getattr(subprocess, ...)`：那些名字只在 Windows 的 `subprocess` / `signal` 里存在，Linux 上取不到，而本模块要在两个平台都能被导入。

已解决的平台缺陷，按「不修会怎样」排序：

- **`tool_system.json` 写死 `平台：linux` / `Shell：bash` / OS 版本三行。** 这是唯一一处不改则其余全部白做的地方：模型判断该写什么语法的首要依据就是这段环境信息，留着它意味着模型在 Windows 上被明确告知自己在 Linux，然后照旧写 bash。改为占位符由 `environment_facts()` 取值，`worker_engine.py` 的**两个**渲染点各接一次（一处服务 Anthropic 协议路径、一处服务非 Claude 注入路径，只接一处的症状是「换某些模型对了、换另一些又不对」）。
- **`DETACHED_PROCESS`。** 详见第三节第 7 项与 `detached_kwargs` 的 docstring，那是本批改动里代价最高的一条。
- **`os.killpg` 在 Windows 上属性不存在**，原代码必然走进 `except` 退化成 `proc.kill()`，只杀直接子进程而留下整棵子树继续占 CPU 与文件锁——一条超时的命令看起来被清理了，实际还在跑。改为 `taskkill /T /F`，`/T` 才是「连同后代」那一半。
- **写死的 `/tmp`。** Windows 上该目录不存在，症状是每条命令在 `open()` 阶段抛 `FileNotFoundError`，而那个异常里没有任何线索指向平台。改用 `tempfile.gettempdir()`。
- **`terminal.py` 的解释器写死 `cmd.exe`**，因此 Windows 用户从持久化终端里永远拿不到 PowerShell。改为 pwsh → powershell.exe → cmd.exe 回退链。
- **`Popen(text=True)` 缺 `encoding`。** 默认走 `locale.getpreferredencoding()`，中文 Windows 上是 GBK，而 PowerShell 的输出编码由 `$OutputEncoding` 决定。两端不对齐的症状是**中文乱码而非报错**，没有任何东西会提示你编码不对。
- **`interrupt()` 的 Windows 分支原先整条为空**，点中断毫无反应且不报错——看起来像命令还没跑完。改用 `CTRL_BREAK_EVENT`，它与 `CREATE_NEW_PROCESS_GROUP` 是一对：缺了那个创建标志，信号会打到 ChatApp 自己身上。
- **禁用命令名单是纯 bash 词汇。** `cat` 与 `ls` 在 PowerShell 里恰好是别名所以原名单部分生效，缺的是 `Get-Content` / `Select-String` / `findstr` 这类原生写法。同时改为大小写与 `.exe` 归一化——PowerShell 大小写无关，精确匹配使整份名单形同虚设。**递归列目录单独特判而不进名单**：bash 里 `ls` 与 `find` 是两个命令，PowerShell 里是同一个 cmdlet 的两种用法，直接入名单会拦掉系统提示词明确要求的裸 `ls`。这是平台间唯一一处无法靠加词解决的不对称。
- **`file_path.split("/")[-1]` 取文件名。** 反斜杠路径上找不到任何 `/`，`[-1]` 因此返回**整条路径**。后果不只是摘要变长：过时读取的「已省略，概括为：…」替换文本会进入模型上下文，而标记旧读取为过时的那几处会遍历整个会话历史逐条重写，所以同一条绝对路径在上下文里出现的是多次而非一次。改用 `os.path.basename`（Windows 的 ntpath 把两种分隔符都认，Linux 的 posixpath 只认 `/` 而那里本来就用 `/`，所以是严格改进）。有 `test_no_file_path_is_split_on_forward_slash_only` 守着，因为这个写法在 Linux 上完全正确、下一个人不会收到任何提示。
- **裸 `pip install`。** PDF 分支在 PyMuPDF 缺失时自动装包，原先调 PATH 上的 `pip`——而它未必属于跑应用的那个解释器。装进错误环境之后 `import fitz` 照旧失败，报出的是「安装失败」或第二次 ImportError，两者都不指向「装到别处去了」。改用 `sys.executable -m pip`，有 `test_pip_is_never_invoked_as_a_bare_executable` 守着。这条与本文档第三节第 5 项的解释器教训是同一件事。

**Bash schema 新增 `shell` 枚举参数（bash / powershell / pwsh / cmd）而不是新增一个工具名。** 后者需要同步八处白名单（`message_edit.py`、`response.py` 两处、`tool_accept.py` 两处、`chat.js` 等），而其中 `chat.js:392` 那份**已经漂移了**——它缺 `缩减读取`，Python 侧两份都有。再加一个名字只会加速这种漂移。

结果摘要里的解释器 label 取值恰好等于该参数接受的四个名字，所以看到 `cmd: dir` 就知道可以写 `shell=cmd`。`_exe_label` 剥掉扩展名正是为此：Windows 上 `which` 返回的后缀大小写由 PATHEXT 决定，实测拿到的是 `pwsh.EXE`。

### 前端视觉体系

新增 `libs/tokens.css` 作为唯一的样式来源，采用 Material Design 3 的令牌命名（`--md-sys-*`）配 Adwaita 取值：主色 `#3584e4`、圆角收紧一档（按钮 6px、卡片 8px、窗口 12px、对话框 16px）、阴影不透明度压到 0.08–0.22 并改由 1px 描边承担分界。明暗双主题的令牌均已备齐。

所有组件样式引用令牌而不写死数值，因此换主题或调色是单点改动。`libs/styles.css` 追加了可复用组件类（按钮四变体、图标按钮、chip、对话框骨架、偏好行等）。

### emoji 替换

新增 `libs/icons.js`，提供 `mdIcon(name, size)` 返回内联 SVG。图标为 24dp 网格上的描边式自绘几何体，`stroke="currentColor"` 因此自动继承宿主颜色并跟随主题，零网络请求、零字体加载。命名沿用 Material Symbols，将来若替换为官方 path 数据是纯值改动。

前后端 emoji 均已清零。实际处理 91 个**码点**而非本文档原先写的 79 个**视觉字符**：差额是 VS16 变体选择符（`⚠️` 是两个码点）与三种转义写法。唯一保留的是 `libs/chat.js` 注释里作为说明引用的一对符号——那段注释在解释为什么抽象图标承载不了原先那对 emoji 的区别，删掉符号会毁掉注释本身。

### 新增功能

- **对话刻度盘**（`libs/minimap.js`）：聊天区右缘的无边框竖向导轨，横向刻度长短区分用户与助手，被彻底隐藏的气泡用 error 色。刻度**等距**排列，右侧一个三角标记当前位置、对齐视口内最上面一条气泡的刻度；悬浮任一刻度立刻弹出气泡预览。等距不损跳转精度——点击一直是按 id 找元素再滚动，从不参考刻度自身位置。刻度厚度与命中区宽度随间距自适应，否则长对话下轨道会糊成实心条且 hover 会命中下方邻居。滚动由 `_mmScrollToEl` 自绘动画（分段缓动，首尾各 16% 缓冲、中段匀速，110–260ms），不用 `scrollIntoView`：后者时长随距离增长且整段都在加减速。
- **侧边栏拖拽调宽**：180–520px，落盘 `localStorage`。
- **会话拖拽排序动画**：FLIP 实现，渲染前记录位置、渲染后用 transform 拉回再过渡到零。

### 缺陷修复

- 分组内会话拖拽排序无效——渲染循环按 `session_ids` 数组顺序遍历而不读 `order` 字段
- 会话拖拽被锚点劫持——标题是 `<a>` 元素，浏览器默认可拖拽，父容器的 `draggable=true` 不覆盖它
- `order` 中点计算自我抵消——批量创建的会话共享同一默认值，中点等于已有值使后端写入成空操作
- 侧边栏按钮溢出容器——`.md-button` 缺 `min-width: 0`
- 会话标题只显示两个字——同一原因，flex 兄弟抢占空间
- 状态推送重渲染丢失侧边栏滚动位置
- 思维链字段名分裂：写入方用 `cc_type`，五处读取方用 `tool_type`，条件恒为假构成死代码

#### 关于字段名分裂的细节

这是本次唯一改变后端行为的修复，重启后会有可感知的变化，请勿误判为回归。

`api/worker_engine.py` 创建思维链气泡时写入的是 `cc_type='thinking'`，而以下五处读的是 `tool_type`，判断永远为假：

- `libs/chat.js` — 思维链识别与吸附目标判定（两处）
- `api/state.py` — 风格过滤跳过判定
- `api/state.py` — 脱水分支的排除判定
- `api/response.py` — 提取前序思维链内容、重试目标校验（两处）

修复后复活的四项行为：

1. **思维链会被正确吸附。** 此前识别完全依赖 `model_name` 以「(思考过程)」结尾这半个条件，不带该后缀的思维链气泡从未被收进回复下方的卡片，会作为独立气泡散落在对话流中。
2. **风格过滤不再改写思维链。** `state.py` 的意图本就是跳过思维链，但键名错误使模型的原始推理文本一直在被后处理篡改——这与项目「你看得到 AI 看到的一切」的设计核心直接冲突。
3. **折叠的思维链能正常展开。** 脱水判定的保护失效，导致不带后缀的折叠思维链被剥掉 `content` 推送到前端，展开后是一张空卡片。这类症状极易被误判为 `chat.js` 的渲染缺陷。
4. **对思维链气泡点重试会被拒绝。** 此前那道校验恒为真，形同不存在。

采用直接替换而非双键兼容读法的依据：`data/sessions/` 下的持久化会话文件中 `"tool_type"` 出现次数为 **0**，`"cc_type"` 出现 86 次（8 个文件），说明前者从未被任何版本写入过，不存在需要兼容的历史数据。

如果某一项修好后表现反而不如从前，那说明那段死代码本身逻辑有问题——它从未执行过，所以从未被验证。请单独检视该段逻辑，不要把键名改回去：键名不一致本身是确定的错，不该用它掩盖别的问题。

---

## 三、未完成的工作

按可安全独立推进的程度排序。

### 1. ~~后端 emoji~~（已完成）

已清零，留此条只为记下统计口径这个坑。

**统计脚本必须同时匹配三种写法**：字面字符、`\uXXXX`、`\UXXXXXXXX`。原文只写了前两种，而 `worker_engine.py` 把 🎨 写成八位大写的 `\U0001f3a8`，只认四位小写会静默漏掉三处——同一个文件先被报为 6 处、再被报为 18 处、实际 21 处，每次都是口径不全。

改动时唯一需要单独判断的是 `api/state.py` 的虚拟收藏会话名：它是会话标题、显示在侧边栏，不是日志。删掉星号是安全的，因为 `main.js` 的该分支已经注入 `mdIcon('push_pin', 14)`，星号其实是第二个图标。

### 2. 箭头字符迁移（34 个字符，约 10 个站点）

`▶`（U+25B6）、`▼`（U+25BC）、`▴`/`▾` 不属 emoji 区段，是单色几何字符，字形随系统字体变化、尺寸不可控。

**本文档原先写「6 处」，那是切换站点数而非字符数。** 两个量都要看，但用途不同：**站点数决定工作量**（每个站点是一次 `textContent` 到 `innerHTML` 的迁移，也是一次需要单独验证的行为改动），**字符数只决定要改多少个字面量**。

实测箭头共 **33 个**：

| 文件 | 字面 | `\uXXXX` 转义 | 合计 |
| --- | --- | --- | --- |
| `libs/chat.js` | 18 | 6 | 24 |
| `frontend.html` | 6 | 0 | 6 |
| `libs/models.js` | 2 | 0 | 2 |
| `libs/panels.js` | 1 | 0 | 1 |

`chat.js` 里除了原文列举的五个站点，还有 L536 附近折叠按钮的 `▴`/`▾`。按码点分布是 `▶` 14、`▼` 8、`▾` 8、`▴` 3。

**两处容易误判：**

1. **`libs/editops.js` 里那个几何字符不算。** 它是 `U+25A0 ■`，在 L161 的上下文构成头部当图例色块用（`<span style="color:...">■</span>` 后接分类名与占比），与展开折叠无关。按几何图形区（U+25A0–U+25FF）机械扫描会得到 34 个，减掉它才是箭头的 33 个。
2. **统计必须同时数字面字符与 `\uXXXX` 转义。** `chat.js` 的 6 个转义分布在脱水气泡、独立 tool_result 与 `_expandStateMap` 恢复逻辑那几处，只搜字面符号会漏掉、只按字面数会得到 28。这条与第 1 项的口径教训是同一个，而本轮在**校验它的时候又踩了一次**——先用 `grep -c` 数了匹配行数（19），再用只认字面字符的脚本数了 28，两次都以为文档写错了。

改成 SVG 需要把这些位置从 `textContent` 整批迁到 `innerHTML`。**这一批动的是展开态保护机制**（见 `UI_CONVENTIONS.md` 第五节第 2 条），建议单独成一个提交并逐处验证，不要与其他改动混在一起。`tests/test_dom_render.py` 里的展开态相关用例可以在改动后直接复用来验证。

### 3. ~~语法高亮的暗色适配~~（已完成）

`libs/styles.css` 顶部已追加整套 a11y-dark 覆写，`.tool-params-hljs` 三条也补了 GitHub-dark 对应值。整套换而非逐值调的理由：两套主题各自按自身底色做过 WCAG AA 校验，只换一半会得到唯一一种比两者都糟的状态——部分 token 可读、部分不可读，且无从分辨哪些是哪些。

改动这一块时两条约束容易踩：

1. **刻意不设 `.hljs{background}`。** 上游 a11y-dark 会设 `#2b2b2b`，但本项目代码块底色来自 `.content pre` 的 `var(--md-sys-color-surface-container)`，写死等于把每个代码块钉在一个固定灰上并忽略令牌。
2. **两条规则必须带后代选择器。** 亮色主题给 `.hljs-class .hljs-title` 与 `.hljs-tag .hljs-attr` 着色的特异性是 0,2,0，压过单类暗色规则的 0,1,1。缺了它们，类名与标签属性在暗色下仍是亮色值——这是纯特异性问题，看渲染结果很难反推成因。

### 4. ~~暗色主题入口~~（已完成，另附主题色）

入口在全局设置的「外观主题」：明亮 / 暗色 / 跟随系统三档，另加七个主题色。偏好存 `localStorage` 而非 `global_settings`——配色属设备偏好，同一会话在台式机看亮色、手机看暗色是合理的，而 `global_settings` 每次变更都要往服务端走一趟。

`frontend.html` 头部有一段**内联且同步**的引导脚本，必须保持在那里：所有 `libs/` 脚本都在 body 末尾加载，从那里应用暗色会先画一帧亮色再翻转，也就是可见的闪白。它包在 `try/catch` 里是因为隐私模式下 `localStorage` 访问会抛异常，而这段位于任何其他脚本之前，抛出会阻断整页解析。两处键名（`theme_mode`、`theme_accent`）与 `settings.js` 的 `applyTheme()` 共用，改一处要改两处。

主题色只声明一行种子色，容器色对由 `tokens.css` 末尾的 `color-mix` 规则派生——7 个色 × 2 个方案 × 6 个令牌是 84 个手写色值，必然出错。**混合比例是从既有 Adwaita 蓝反解出来的**：`#3584e4 → #99c1f1` 在 R/G/B 三通道分别得 0.505/0.504/0.52，即均匀 50% 混白。这个比例已被 `tests/test_dom_render.py` 实测确认（暗色绿色种子 `#3a944a` 得 157/202/165，与理论值零误差）。默认蓝仍显式重述手调值，因此既有安装的渲染结果逐像素不变。

**一处已知取舍，不是缺陷。** 七个种子色对白字的对比度为蓝 3.77、青 3.79、绿 3.85、橙 3.42、粉 3.50、灰蓝 3.91、紫 5.83，除紫色外均低于 WCAG AA 对正文的 4.5。但既有的 Adwaita 蓝本身就是 3.77——Adwaita 整套按「UI 组件 3:1」而非「正文 4.5:1」取值，这是配色体系的既定选择，新增色相与项目原有默认处于同一水平，并非新引入的回归。若要达 AA，正确做法是把 `on-primary` 从固定白改为按种子色明度二选一，但那会同时改变现有蓝色主题的按钮文字颜色，属产品判断。

### 5. ~~测试~~（122 passed / 2 skipped）

**先确认你用的是哪个解释器，这不是脚注。** `pytest` 装在哪个 Python 里与 ChatApp 跑在哪个 Python 里是两件事，而它们不一致时的表象是「无输出加退出码 1」——那与测试内容毫无关系，纯粹是模块找不到。本文档原先写「`pytest 9.1.1` 在 conda 环境 `deep_lea` 中」，那只在最初那台机器上成立；在 Windows 那台上根本没有 conda，而 PATH 上的 `python` 解析到一个没装 pytest 的 miniconda 环境。

所以第一步永远是问出解释器的身份：

```bash
python -c "import sys, importlib.util as u; print(sys.executable); print(u.find_spec('pytest') is not None)"
```

拿到确切路径之后再跑，必要时写全路径：

```bash
python -m pytest tests/ -q                 # 全部
python -m pytest tests/ -q -k "not dom"    # 只跑纯 Python，不需要浏览器
```

Windows 上另外两个开关值得默认加上：`-X utf8` 让子进程按 UTF-8 写 stdout（测试里的断言消息与 docstring 都是中文，走 ANSI 代码页会抛编码错误而表现为无输出），`-p no:cacheprovider` 少一个会 `Path()` 的部件。构成：

| 文件 | 覆盖 |
| --- | --- |
| `test_config.py` | 配置加载与静默降级 |
| `test_style_filter.py` | 过滤规则的确定性与变更记录可回放 |
| `test_message_toggle.py` | 分类顺序、筛选条件、内联思维链可逆性 |
| `test_cross_language_consistency.py` | 前后端分类逻辑的一致性 |
| `test_static_assets.py` | CSS 花括号、令牌引用、缓存版本号 |
| `test_prompt_resolution.py` | 14 个用例，`prompts/` 默认值与 `data/` 覆写的解析顺序、缓存失效、零写入 |
| `test_platform_shell.py` | 40 个用例，两平台分支各自的 argv 与 Popen kwargs、label 与 prelude 的链路一致性 |
| `test_dom_render.py` | 30 个浏览器内 DOM 用例，跑 `harness/render.html` |
| `test_dom_sidebar.py` | 10 个用例，跑 `harness/sidebar.html`，覆盖 `main.js` |
| `test_action_api.py` | 12 个用例，Flask test client 覆盖 `/api/action` |

**后两个文件各自打开了一片此前完全无覆盖的区域，而「此前为什么测不了」是最值得记下的部分。**

`main.js` 是项目最大的文件（2231 行，会话渲染、拖拽排序、标签栏、整条状态更新流水线），此前零覆盖，因为有**两道**障碍而不是一道：三处顶层 `getElementById(...).addEventListener` 对缺失元素抛 TypeError（现已收进 `initApp`）；以及它用 `const chatContainer` / `let currentHistory, bubbleCache, globalSettings` / `const socket` 声明的五个名字，与 `render.html` 为 chat.js 准备的同名 `var` 声明构成 **SyntaxError**，整个文件根本不会被求值。后者是新建 `sidebar.html` 而非扩充既有 harness 的全部理由。

`socket` 的声明因此从 `const io()` 改成了「已有则复用」的 `var`。**不要改回 `const`**：文件里有八处顶层 `socket.on(...)`，harness 必须在 main.js 之前提供一个可挂载的对象。

后端此前零覆盖是因为 `import app` 会在模块作用域构造 `Api`，而 `Api.__init__` 读 `CHATAPP_DATA_DIR`、`makedirs` 并加载该目录下每一个会话文件。`test_action_api.py` 在 `import app` **之前**把该变量指向 `tempfile.mkdtemp()`——**这个顺序不可调换**，否则就是直接读用户的真实对话记录，首次 save 还会写回去。

`-k "not dom"` 的口径仍然成立：`test_dom_sidebar.py` 名字里带 dom 会被排除，`test_action_api.py` 是纯 Python 不需要浏览器。

**四条不写下来就会被「整理」掉的约束：**

1. **`sys.path` 修正留在 `tests/conftest.py`，不要搬到根目录 `pytest.ini`。** 仓库根必须先进 `sys.path`，`import api.*` 才解析得到；pytest 默认的 prepend 模式插入的是**测试文件所在目录**，也就是 `tests/`，深了一层。放在 `conftest.py` 里让测试目录自成一体——复制它、或从别处只跑这个目录，都不需要外部文件配合；根目录 ini 会把这份依赖藏在目录之外。
2. **DOM 用例不启动 Flask。** 它们跑 `tests/harness/render.html`，只加载真实的 `libs/` 脚本与样式表。启动真实服务会读写 `data/sessions/`，那是用户的真实对话记录，不是测试夹具。
3. **harness 的桩必须排在应用脚本之前，且不能与被测代码同名。** `utils.js` 在加载时就执行 marked 配置的 IIFE，`chat.js` 与 `minimap.js` 在文件末尾立即挂事件，晚定义的桩来不及被看到。同名更糟：第一版给 `rejectApproval` 写了桩，而 `codeblocks.js` 也声明了同名全局函数且加载在后——后者胜出，桩**静默失效**，测试看起来在验证审批拒绝，实际验证的是一段从未执行的代码。现在改为断言真实的 `postAction` 载荷，覆盖面反而更大。
4. **Playwright 属开发期依赖，缺件必须跳过而非失败。** 见第六节。

**一条对项目有用的浏览器事实：** `getComputedStyle` 对普通十六进制值返回 `rgb(r, g, b)`，但对 `color-mix()` 的计算结果返回 `color(srgb 0.61 0.79 0.65)`（CSS Color 4 形式，通道 0–1 浮点）。两种都合法，任何解析计算样式的代码都得同时处理。

### 6. ~~性能优化~~（4 处全部完成）

四处都在渲染核心路径上，改动全程由 `tests/` 兜底——没有那层保障不建议动这一区。

- **~~`libs/codeblocks.js` 末尾的 500ms 轮询~~（已整段删除）。** 本文档原先说它「功能已被 `chat.js` 的渲染时逻辑完整覆盖，可整段删除」——读完之后结论更强：**它不只是冗余，而是在制造一个点了没反应的假按钮。** 三条证据：（一）它靠 `nextEl.getAttribute('data-approval-reject')` 判重，而 `chat.js` 只在**按钮**上设 `data-testid`、包裹 div 上没有该属性，所以判重永不成立、每半秒插一个；（二）它用 `allBubbles[j] === bubble` 求 DOM 位置当 `history` 数组下标，而 `renderChat` 会跳过被吸附的消息、思维链卡片又是 `.th-card` 不是 `.message-bubble`，两者必然不同；（三）错误下标传到 `app.py` 的 `cc_reject_approval` 后取不到对应 `part_id`，`_rej_tool_use_id` 保持 `None`、整段跳过且不报错。外加它的按钮写死 Bootstrap 红、不跟主题。`tests/test_dom_render.py::test_approval_reject_button_is_never_duplicated` 是对这次删除的直接实证——轮询在场时它会在 500ms 后失败。
- **~~`libs/main.js` 的 100ms `setInterval`~~（已改为按需启停）。** 拆成 `_waitTimerId` 状态位、幂等的 `_startWaitTicker` 与自带停机条件的 `_waitTick`；由 `renderChat` 末尾启动，元素归零时自行 `clearInterval`。**心跳防丢包跟着一起停是正确的，不要「修」回常驻**：`syncCounter` 本来就只在有等待气泡时累加，计时器停下的时刻恰好是心跳本该静默的时刻。启动点选 `renderChat` 是因为它是 `.waiting-time` 元素的唯一生产者，「从无到有」这个方向由生产者覆盖才不会漏。
- **~~`libs/chat.js` 的 `msgHash`~~（已改为双 32 位累加器）。** 字段与采样口径完全不变，因此判定灵敏度不变；省掉的是每条消息每次渲染约两百字符的中间串。**`Math.imul` 是正确性必需而非风格偏好**：32 位乘积会溢出双精度尾数，普通 `*` 静默丢掉的正是低位，也就是哈希唯一依赖的部分——改回 `*` 不会报错，只会让缓存判定偶发失灵。用两个累加器而非一个的理由是单个 32 位摘要按生日界在约 8 万个不同值时开始碰撞；但这里不是生日问题，每条消息只与自己上一次比较，所以 64 位使单次比较的碰撞概率落在 $2^{-64}$ 量级。
- **~~`libs/chat.js` 的 KaTeX 扫描~~（已加 `_hasMath` 预判）。** 判定读 `msg.content` / `diff_content` / `content_parts` 而非 `bubble.textContent`：后者虽然一定准确，但会为长气泡多分配一份完整副本，把省下的开销又花掉一部分。工具结果与 subagent 正文来自别的消息，主气泡的字段扫描覆盖不到，因此在各自的 append 处单独置标记；subagent 那处取无条件置真，因为它数量极少而漏判的代价是公式永久不渲染，两侧不对称。

### 7. Windows 上尚未验证的三项

一次性命令执行（`execute_bash` 那条链路）已在真机上确认可用：默认解释器走 pwsh 7、stdout 正常回收、中文不乱码、无窗口闪现。以下三项**没有任何真机证据**，因为它们只能在界面交互中暴露。

**持久化终端。** 开一个终端、执行一条命令。三种失败各有不同表现：

- 终端一直停在「运行中」不回到空闲 → `-Command -` 没有逐行执行而是缓冲到 EOF，哨兵 `__TERM_DONE__` 永远等不到。这一种最严重，等于整个终端功能不可用。
- 输出正常但每条命令之后多出几行奇怪内容 → `function prompt { '' }` 没把提示符从 stdout 清干净。PowerShell 把提示符写进与命令输出同一条流，这是唯一能从进程内部关掉它的办法。
- 桌面上多出一个**常驻**的控制台窗口（不是闪一下）→ `CREATE_NO_WINDOW` 在长期存活的交互式进程上不成立。回退办法写在 `new_process_group_kwargs` 的 docstring 里：先摘掉这个标志单独验证，把变量隔离开再判断。

**中断（`CTRL_BREAK_EVENT`）。** 需要先有一个正在跑的长命令再点中断。**第一次务必用无害命令试，比如 `Start-Sleep 30`。** 风险是明确的：若 `CREATE_NEW_PROCESS_GROUP` 因某种原因没生效，那个信号会打到 ChatApp 自己身上——表现是**整个后端被中断**而不是那条命令被停下。用无害命令试，最坏情况也只是重启一次后端。

**后台命令熬过 ChatApp 重启。** `CREATE_NEW_PROCESS_GROUP` 挡得住发往 ChatApp 进程组的 Ctrl+C（也就是「按 Ctrl+C 重启」这个主要场景），挡不住整个终端窗口被关闭时广播的 `CTRL_CLOSE_EVENT`。这是**接受的取舍**而非缺陷：唯一能同时熬过关窗的标志是 `DETACHED_PROCESS`，而它会让 PowerShell 完全不执行命令（见下一节）。没有输出的后台命令是纯粹的浪费，所以选择保住输出。

**`os.execv` 的语义在 Windows 上不同（已定位，刻意未改）。** 它出现在 `updater.py:208`、`updater.py:241` 与 `app.py` 的启动更新分支。POSIX 上它**替换**当前进程映像，PID 不变；Windows 上没有这个语义，CPython 的实现是新起一个进程然后让原进程立即退出——于是父进程句柄失效、控制台归属混乱，在被 shell 启动的场景下表现是「命令看起来结束了但服务在后台继续跑」。

没改的三条理由：它只在 `enable_auto_update` 开启**且**确实发现新版本时才触发；正确的修法需要一个产品判断而非技术选择（改成 `subprocess.Popen` 加 `sys.exit`，还是干脆打印「更新已应用，请手动重启」——后者更诚实但改变了自动更新的体验）；以及它无法在不真正触发一次更新的情况下验证，而伪造一次更新会动 `version.json` 与 `data/updates/`。

要修的话两个候选都记在这里，免得下一个人重新推导：`Popen` 加 `sys.exit(0)` 保留自动重启但父子关系与原来不同；打印提示并退出则把重启交给用户，代价是「自动更新」名不副实。

---

## 四、待决策的问题

以下均为已定位但刻意未动的事项，每项都写明当前行为，请勿当作遗漏随手改动。

### ~~气泡右键菜单只显示一项~~（已决策：菜单整个移除）

原状况：`contextmenu` 处理器前九行用 `items +=` 累加了十项，最后一行却是 `items = ...`——赋值而非累加，把前面全部丢弃，因此菜单实际只显示「折叠/展开」。

**决策是第三条路：菜单不要了，右键直接折叠。** 那个菜单在覆盖赋值之后本来就只提供折叠一项，中间隔一层菜单纯属多一次点击；十项操作全部仍在气泡上方的悬停操作栏里，没有丢任何能力。相关死代码已清理：`frontend.html` 的空 `#bubble-context-menu` 容器、`main.js` 里关闭它的处理器、`styles.css` 共用规则里属于 `.bubble-ctx-item` 的那一半（`.tab-ctx-item` 仍在用，只能删逗号后面）。

**处理器里那道选区判断不是多余的防御，删掉是回归。** 气泡正文是 `user-select: text !important`，右键是访问原生复制菜单的唯一途径。原处理器虽然挡住了原生菜单，但至少不破坏选区；改成直接折叠后若不放行，用户选中一段话想复制会得到「气泡折叠 + 选区丢失」，比改动前更差。判断范围限定在本气泡内（`bubble.contains(sel.anchorNode)`），别处的选区不影响本气泡。三个 DOM 用例覆盖了这三种情形。

### ~~确认强度与实际后果反相关~~（已决策：改可恢复性，不加确认）

原状况：重试一次 Bash 有二次确认（最严），而**删除一个会话没有任何确认**——后者销毁的是一整份对话记录，也就是本文档末节明确称为「用户资产」的东西。

**决策不是给所有破坏性动作都加确认**，那会训练用户无脑点确认从而让确认整体失效。做法是改变可恢复性：

- 侧边栏新增回收站。`delete_session` 一直是软删除（设 `soft_deleted`，前端到处过滤），但此前**没有任何恢复途径**，所以删掉的会话除了手改 JSON 无法取回。现在 `restore_session` 补齐了这一半，删除因此不再需要确认。
- 无副作用工具的重试免确认。判据是「重跑一次是否改变本地状态」而非速度：`CC_RETRY_NO_CONFIRM` 收录 Read / Grep / Glob / WebSearch / WebFetch。往这个集合里加名字等于断言重跑它无害，加错了用户会以为有确认而实际没有。

**整个应用现在不存在销毁对话记录的代码路径**，这是刻意的。从 `self.sessions` 移除条目正是让 `save_sessions` 的孤儿清理删掉磁盘文件的动作，所以任何 purge 方法都会是唯一一条毁掉 transcript 的路径。`test_action_api.py::test_there_is_no_purge_endpoint` 就是为了让「后来又加回来」变成一次失败而非一次意外。

**被接受的后果：`data/sessions/` 下的文件只增不减。** 软删的会话始终留在 `self.sessions` 里，孤儿清理因此永远扫不到它的文件。看到目录堆积**不要**当成清理逻辑坏了——回收空间是用户在应用之外自己决定的事。

### 分组内条目拖到组外不带排序

当前只发出 `remove_session_from_group`，因此移出后条目会按原有 `order` 落在未分组区的某个位置，而不是松手的地方。

要改需要在 `list.ondrop` 里补一次 reorder，但拖到空白区域时没有参照条目可用。倾向的做法是取未分组区最后一个条目的 `order` 加 1，即「移出即置末」，语义清楚。

### 后端 `is_processing` / `autopilot_active` 可能一直为真

**本文档此前没有记录这一项，它只存在于 `libs/kanban.js` 的两处注释里**（缓存填充处与托管区段渲染处）。那两处注释写明这两个字段有「做完之后仍然保持为真」的已知缺陷，因此前端不敢直接读 `autopilot_active`，改用 `autopilot_active && (is_processing || active_threads > 0)` 作为「确实在工作」的真值判断。

**看到那个绕过写法不要当成冗余判断简化掉**——简化之后看板的托管区段会一直显示为进行中，而且这个症状看起来像看板渲染错了、不像后端状态没清。

要真修需要动 `api/autopilot.py` 与 `api/tool_accept.py` 的状态流转，影响面远超前端配色一类的改动，因此本轮没碰。也可能上一位开发者同样判断为不值得动、只是没记录下来。

### ~~刻度盘的可选增强~~（两个想法均已采纳）

原先记的两条都已落地：`mm-pending` 用刻度左侧的圆点标记仍有 `pending` 或 `executing` 工具的气泡，`mm-omit` / `mm-collapse` 用两档透明度（0.5 / 0.7）区分概括模式与折叠。

**三个视觉维度现已全部占用**：宽度与填色表达角色或排除、透明度表达上下文削减程度、圆点表达未确定工具。想表达第四种状态必须另找维度，复用这三者中的任何一个都会产生无法分辨的组合。

两处不要「简化」：

- **圆点必须是 `::before`，不能是子节点。** `minimap.js` 对裸轨道点击按 `querySelectorAll('.mm-tick')` 的扁平位置取下标，多一个兄弟节点会让每一次跳转都偏移；`::after` 已被命中区占用。它还锚在刻度而非轨道固定偏移，这样 hover 改变宽度时间距不变。
- **两条透明度规则只发给未隐藏的行。** `mm-hidden` 自身已经设了 opacity，两者并存时胜负由源码顺序决定，属于不可靠的隐式依赖。

---

## 五、验证方法

这个项目没有构建步骤。现在有两层保障：`tests/` 下的 122 个纯 Python 用例（另有浏览器内 DOM 用例，缺 Playwright 时跳过），加上下面这些静态检查。

用例数会随改动变化，别把它当断言看——真正的判据是第三节第 5 项那张表，以及跑一遍的结果本身。

**新增检查时把逻辑写进 Python，不要写进 shell。** 下面几条静态检查里原先有两条是 bash 专属写法（通配展开与 `for`/`case` 循环），搬到 Windows 上一条也跑不了。把遍历、过滤、判定都放在 `python -c` 里，命令在两个平台上就逐字相同，顺带避开 cmd 的引号转义规则与 PowerShell 不为原生命令展开通配这两个坑。

### 测试

```bash
python -m pytest tests/ -q
```

第三节第 5 项有构成说明。`test_static_assets.py` 已经把下面的「CSS 花括号收支」与「令牌引用完整性」两项固化成用例，因此那两条现在是自动执行的。

**测试不能替代静态检查，两者覆盖的是不同的东西。** `node --check` 验语法、测试验行为；尤其是**标识符改名**这类错误，解析检查完全无能（函数改名后仍有旧名调用是运行时 `ReferenceError`，解析阶段看不出来），而 DOM 用例只覆盖被它触及的那条路径。本分支两次改名（`_mmUpdateViewport → _mmUpdateCursor`、`bgColor → _barBg`）的安全都来自 grep 交叉验证，不是来自测试。

### JavaScript 语法

bash / zsh：

```bash
for f in libs/*.js; do case "$f" in *.min.js) ;; *) node --check "$f" ;; esac; done
```

PowerShell（上面那条用的 `for` / `do` / `case` / `esac` 全是 bash 关键字，在 PowerShell 与 cmd 下不是行为差异而是根本无法解析）：

```powershell
Get-ChildItem libs\*.js | Where-Object { $_.Name -notlike '*.min.js' } | ForEach-Object -Process { node --check $_.FullName } -End { Write-Output 'node --check finished' }
```

两条并列而非替换：这个仓库现在同时服务两个平台。

`-End` 那段不是装饰，理由同下面 Python 那条的 `print`：`node --check` 成功时不打印任何东西，于是「全部通过」与「管道压根没执行」产生一模一样的表象。用 `ForEach-Object -End` 而不是分号拼一个 `Write-Output`，是为了让它留在同一条管道里——不引入第二条命令，也就不会成为「反正可以随便拼」的先例。

`node --check` 只做解析不执行、无副作用。它比朴素的括号计数可靠得多——后者会被正则字面量里的字符类（如 ``[^`\n]``）触发假阳性。

**node 属额外依赖。** 它不在 `requirements.txt` 里，应用本身也不需要它（项目明确宣称无 Node.js 构建步骤）。所以它缺席时这条检查做不了，而不是项目坏了——遇到「找不到 node」不要去装构建工具链，那是另一回事。

### Python 语法

**通配展开要交给 Python 做，不要交给 shell。** 原先这里写的是 `python -m py_compile app.py config.py api/*.py`，那在 bash 下可行，但 PowerShell 对原生命令**不做**通配展开——Python 会收到字面的 `api/*.py` 这个不存在的路径。下面这条在两个平台上逐字相同：

```bash
python -c "import glob, py_compile, sys; fs = ['app.py', 'config.py'] + sorted(glob.glob('api/*.py')); [py_compile.compile(f, doraise=True) for f in fs]; print('py_compile OK:', len(fs), 'files')"
```

这与项目自身 `restart_server` 动作采用的手段一致——那一处本来就是在 Python 里 `glob.glob("**/*.py", recursive=True)`，所以这不是新做法，是把文档对齐到代码既有的做法。

**用跑应用的那个解释器，不是 PATH 上的那个。** 理由同第三节第 5 项：不同 Python 版本对语法的接受范围并不完全一致，在 A 上编译通过不保证在 B 上导入不炸。

末尾那句 `print` 不是装饰。`py_compile` 成功时本来就不打印任何东西，而「无输出加退出码 0」在这个环境里至少有三种完全不同的成因（见下面第五节末的第 1 条）——给它一个正向证据，才能把「通过了」与「根本没跑」区分开。

`api/response.py` 或 `api/state.py` 一旦有语法错误，应用在 `import api` 阶段就会崩溃、连启动都做不到。

### CSS 花括号收支

必须归零。**一旦失衡，浏览器会静默丢弃后续全部规则且不报任何错**，症状是「部分样式没生效」却查不出原因。

### 令牌引用完整性

提取所有 `var(--md-sys-*)` 引用，比对 `tokens.css` 与 `styles.css` 中是否有对应定义。**CSS 变量名拼错不会报错，只会静默失效**，是最难发现的一类问题。

### HTML 标签配对

用 `html.parser` 追踪标签栈。`frontend.html` 含十余段嵌套内联 SVG，标签配对出错时浏览器会静默纠错成完全不同的 DOM 树，症状是元素位置诡异而控制台无任何输出。

### 四条关于验证手段本身的教训

这几条不是关于代码，而是关于「怎么知道一件事真的发生了」。它们各自都曾让我基于虚假证据下结论。

1. **「退出码 0 且无输出」不能当作成功。** 至少三种完全不同的原因会产生一模一样的表象：`DETACHED_PROCESS` 下的 PowerShell 启动后 host 初始化失败、不执行任何命令、以 0 退出；解释器里没装 pytest 时 `-m pytest` 的失败信息去了别处；以及 `py_compile` 成功时本来就不打印任何东西。**每次需要确认某件事真的发生了，都要求一个正向证据**：一个被打印出来的标记、一个被创建的文件、一个可以读回的版本号。诊断脚本里那个 `side_exists` 字段就是这个思路，它当场把「没执行」与「执行了但输出丢了」分开了。
2. **同一个文件名不能作为反复读取的诊断出口。** Read 工具的防重机制会在第二次读取时返回一段 system-reminder 而不是新内容，而那段文本长得很像正常返回，容易被当成「文件没变」。诊断产物要么直接打 stdout，要么每次换文件名。
3. **跨平台的测试断言必须两个方向都打补丁。** `tests/test_platform_shell.py` 原先只给 Windows 方向打补丁、POSIX 方向靠「开发机恰好是 Linux」，那等于把开发机的平台悄悄写进了测试前提——搬到 Windows 上之后八条用例集体反向失败。现在有对称的 `nt` / `posix` 两个 fixture，外加独立的 `no_powershell`（平台是 Windows 不代表装了 pwsh）。**凡是伪造出来的通过都在各自 docstring 里写明**：把「伪造出来的通过」误当成「真机验证过」是这类测试最容易造成的伤害。
4. **含中文的提交信息用 `git commit -F` 读文件，不要用 `-m` 传字符串。** 后者要跨 shell 到 git 的编码转换，而提交信息一旦写成乱码就固化在历史里了，比命令失败难处理得多。写文件这条路径已实测可靠。

### 写检查脚本时的两个注意点

1. **先剥离注释行再统计。** 在代码里写下旧值作为说明（例如注释「原值为 `04px 4px 0`」）会让子串搜索命中注释本身，产生假阳性。
2. **逐行打印而非只报总数。** 单点残留会被淹没在计数里。`state.py` 那处遗漏的 `tool_type` 正是靠逐行输出才被发现。

---

## 六、协作约定

### 提交信息

采用 `type: description` 的 Conventional Commits 格式。类型选择上区分 `fix`（修真实缺陷）与 `refactor`（等价重写）——如果改动包含行为修正就不是 `refactor`。

正文用于记录**根因与实现约束**，不复述代码做了什么。判断标准：如果这条信息删掉后，下一个人会重新踩同一个坑，就该写。

### 静态资源的缓存版本号

`frontend.html` 里所有 `libs/` 引用都带 `?v=N` 查询参数。**改动任何 CSS 或 JS 后必须推进对应的版本号**，且互相依赖的文件要同批推进。

不遵守的症状：改动看似完全无效。更糟的情况是只推进了一半——例如 CSS 加了新类而 JS 版本没动，会得到「类存在但没人引用」或「引用了不存在的类」的中间态，比两边都是旧版更难判断。

### 索引操作串行

`git` 的索引操作（`add`、`rm --cached`、`commit`）不能并发执行，否则会撞上 `index.lock`。

### Playwright 是开发期依赖

**用户运行 ChatApp 不应依赖它，因此测试套件本身也不能因它缺席而变红。** `tests/test_dom_render.py` 用模块级 `pytest.importorskip('playwright')` 加 `browser` fixture 里对 `launch()` 的异常捕获实现「缺件即跳过」：缺 Python 包、缺 Chromium 二进制、缺 Chromium 所需的系统库三种情形都会跳过并给出原因。这条已被实证——首次跑时 Chromium 尚未下载完，结果是 `64 passed, 20 skipped` 而非二十个红叉。

**跳过数目有两种形状，差一个量级，别把少的那种当成用例丢失。** 缺 Python 包时 `importorskip` 在**模块导入阶段**就跳过整个文件，两个 DOM 文件各算一条，总数是 **2**；缺 Chromium 二进制时模块能导入、失败发生在 `browser` fixture 里，于是**逐条**跳过，数目等于受影响的用例数（上面那次是 20）。两者都是设计内的行为。

同一个原因还解释了另一件容易困惑的事：缺 Python 包时，`-k "not dom"` 加与不加的总数**完全相同**——那两个模块从不产出任何 item，没有东西可供筛选。

**Windows 那台机器的状态是个特例，值得记一下**：Chromium 缓存目录已经存在（`%LOCALAPPDATA%\ms-playwright`，大概是另一个 conda 环境装的），但应用解释器里没有 playwright 包。所以在那台机器上要跑 DOM 用例只需 `pip install playwright`，不必再下 115MB 浏览器。

安装浏览器：

```bash
python -m playwright install chromium
```

包约 115MB，下载需要几分钟。**不要加 `--with-deps`**：那会调 apt 安装系统库，属于修改共享环境。如果这台机器缺 Chromium 运行所需的系统库，`launch()` 会失败并被捕获成跳过，届时再单独决定要不要装系统依赖。

### 数据目录

`data/`、`raw_dumps/`、`webfetch_cache/` 均为运行时产物且已被忽略。`data/sessions/*.json` 是用户的真实对话记录，属用户资产——任何批量改写（包括 emoji 清理）都不应触及它们。