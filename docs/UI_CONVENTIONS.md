# 前端样式与图标约定

面向要改动 `libs/` 或 `frontend.html` 的开发者。配套文档：`docs/HANDOVER.md`。

---

## 一、令牌层是唯一的样式来源

`libs/tokens.css` 定义全部设计令牌，`libs/styles.css` 与各业务 JS 只引用不定义。

**唯一的硬规则：组件样式不写死数值。** 颜色、圆角、间距、阴影、字号、动效时长与曲线全部走令牌。这样换主题或调整视觉是单点改动。

命名沿用 Material Design 3 的系统令牌约定（`--md-sys-*`），取值换成 Adwaita，因此整套界面从 Material 紫转向 GNOME 蓝时**没有改动任何一条选择器**。

### 令牌分组

- `--md-sys-color-*` — 颜色语义角色
- `--md-sys-shape-corner-*` — 圆角，六档：extra-small 4px、small 6px、medium 8px、large 12px、extra-large 16px、full
- `--md-sys-elevation-level0..5` — 阴影
- `--md-sys-spacing-0..12` — 间距，4px 基数
- `--md-sys-typescale-*` — 字号、行高、字重、字距
- `--md-sys-motion-easing-*` / `--md-sys-motion-duration-*` — 缓动曲线与时长
- `--md-sys-state-*-opacity` — 交互态叠加不透明度
- `--md-sys-z-*` — 层叠顺序，集中在一处便于审计
- `--sidebar-width` — 侧边栏宽度，由拖拽调宽写入
- `--md-sys-accent-seed` — 主题色种子，由 `<html>` 的 `data-accent` 选定。它是配色方案的输入参数而非语义角色，容器色对由 `tokens.css` 末尾的 `color-mix` 规则从它派生，因此删掉它会让七个主题色全部静默失效

### 与原版 M3 的偏离

均为有意选择，改动前请先理解原因：

- **圆角整体收紧一档。** GNOME 的按钮和列表行是 6px、卡片 8px、窗口 12px。M3 的 28px 对话框圆角是它最具辨识度也最不 GNOME 的特征，降到 16px。
- **阴影压平。** M3 在一层扩散阴影下再叠一层 30% 不透明度的贴身阴影，在 GNOME 的尺度上读起来像脏污。Adwaita 用 1px 描边分隔表面、把真正的纵深留给浮层和对话框。
- **表面阶梯是中性灰。** 不用 M3 那种带主色染色的紫调白。内容区纯白，chrome 低一档。

---

## 二、颜色语义

### 表面阶梯

`surface` 是内容区，`surface-container-lowest` 到 `surface-container-highest` 五档由浅到深。层次优先靠这个阶梯表达，其次靠 elevation，**尽量不用 1px 边框**——但对话框的头部与底部例外，弹窗尺度下色阶差辨识度不足，那里保留分界线（Adwaita 也是这么做的）。

### 容器色对

每个语义色都有四元组：`X`、`on-X`、`X-container`、`on-X-container`。

**用法上最常见的误用是拿 `X` 当背景色。** 正确的组合是：

- 强调元素（图标、指示线、纯色按钮）用 `X` 配 `on-X`
- 有底色的区域（徽章、tonal 按钮、提示条）用 `X-container` 配 `on-X-container`

拿 `error` 当底色再配白字，在暗色主题下会变成深底深字——因为暗色方案里强调色会被提亮一档以满足对比度。**设置背景色时必须同时设置前景色**，只设一个就是留了一个暗色主题下的坑。

### 非规范色

`success` 与 `warning` **不属于 M3 规范**，是本项目为「采纳/拒绝」「运行中/成功」这类状态补的，取值匹配 Adwaita 的绿与黄，因此与官方色板处于同一体系。在 M3 文档里找不到它们是正常的。

语义分配：

- `primary` — 主操作、用户气泡（配 secondary-container）、焦点指示线
- `secondary-container` — 选中态、tonal 按钮、次要标识
- `tertiary` — 辅助流程：思维链、subagent、收藏、图片附件。这一族共用同色是有意的，让用户能认出「这些都不是主线内容」
- `success` / `error` — 操作结果，不要用来表示常规动作
- `warning` — 需要注意的进行中状态：终端运行、托管中、概括模式

### 交互态

用 state layer 叠加而非直接换色：

```css
background: color-mix(in srgb, var(--md-sys-color-on-surface) 8%, transparent);
```

hover 8%、focus 与 pressed 10%、dragged 16%、disabled 内容 38%。这样明暗主题下都自动成立。

---

## 三、图标

`libs/icons.js` 提供 `mdIcon(name, size)`，返回内联 SVG 字符串。

```javascript
el.innerHTML = mdIcon('content_copy', 14) + ' 复制';
```

- 24dp 网格上的描边式几何体，1.8 描边、圆头圆角连接
- `stroke="currentColor"`，自动继承宿主颜色并跟随主题，因此放进任何按钮都不需要额外指定颜色
- 带 memo 缓存，同一图标在长对话里会被生成上百次
- 零网络请求、零字体加载，与 `libs/` 全本地化的既有做法一致
- 命名沿用 Material Symbols，将来替换为官方 path 数据是纯值改动

### 静态 HTML 中的图标

`frontend.html` 里图标写成内联 SVG 字面量而非 `mdIcon()` 调用——静态 HTML 无法在标签内求值，而 `DOMContentLoaded` 时注入会产生可见闪烁。字面量的 path 数据与 `icons.js` 中的定义逐字一致，改动时两处都要改。

### 必须保留文字标签

**这是硬约束，不是风格偏好。** 以下位置通过按钮文本判断状态，做成纯图标按钮会静默破坏它们：

- `libs/actions.js` 的 `toggleAutopilot` — 靠 `innerText.indexOf('停止托管')` 判断当前是启动还是停止
- `libs/codeblocks.js` 末尾的 MutationObserver — 靠 `lbl.textContent.indexOf('申请审批')` 定位目标块
- `libs/panels.js` 的 `renderQueue` — 暂停按钮由 `innerText` 在「暂停」与「继续」之间切换
- 设置弹窗的「展开更多选项」按钮 — 靠 `textContent.indexOf('展开')` 判断折叠态并重写文案

后两处因此**只换了形制没有加图标**。

纯图标按钮（如工具结果行的四个操作按钮）必须补 `title` 属性：emoji 本身多少能传达语义，抽象几何图标不行，没有 tooltip 就是可用性退步。

---

## 四、可复用组件类

定义在 `libs/styles.css`。新增 UI 时优先复用，不要另写内联样式。

### 按钮

`.md-button` 加一个变体修饰符。变体对应语义强度，不是随意挑颜色：

- `--filled` 主操作，`--tonal` 次要但需强调，`--outlined` 低强度，`--text` 最弱，`--danger` 破坏性
- 尺寸修饰符 `--compact`（32dp 高）、`--block`（占满宽度）
- `.md-icon-button` 纯图标，40dp 正圆，`--compact` 为 32dp

### 其他

- `.md-chip` — filter chip，`--selected` 为选中态
- `.md-modal-overlay` / `--header` / `--close` / `--footer` / `--body` — 对话框骨架，五个弹窗共用
- `.settings-item` / `-label` / `-title` / `-desc` / `-grid` — GNOME 风格偏好行
- `.sm-context-menu` / `.sm-ctx-item` / `.sm-ctx-danger` — 菜单，项目内五处菜单共用以保持一致
- `.status-tag` 加 `.tag-omit` / `-collapse` / `-annotated` / `-hide` — 状态徽章
- `.th-card` / `-header` / `.th-body` / `.th-open` — 思维链卡片
- `.inline-thinking-block` / `-header` / `-body` — 内联思维链
- `.mm-tick` / `.mm-user` / `.mm-assistant` / `.mm-hidden` / `.mm-cursor` — 刻度盘，`.mm-cursor` 是位置三角；修饰类 `.mm-omit` / `.mm-collapse`（两档透明度）与 `.mm-pending`（`::before` 圆点）叠加在同一刻度上
- `.conn-banner` / `-detail` — 断线横幅，由 `main.js` 的 `_connBanner` 建在 `<body>` 下。**不能建在 `#chat-container` 里**，`renderChat` 会按位置 diff 并移除多余子节点
- `.dh-skeleton` / `--error` — 脱水工具结果的加载中与加载失败态，挂在带 `data-dh-row` 的那一行上
- `.cb-abort` — 只做选择器标记，不提供任何外观。中止按钮同时带 `.cb-reject`，而同行的批量禁用用 `.cb-reject:not(.cb-abort)` 把它自己排除出去；**看起来冗余，删掉会让中止按钮在点击后把自己也禁用掉**
- `#mm-preview` / `-head` / `-body` — 刻度悬浮预览，由 minimap.js 建在 `#main-area` 下
- `.md-num` — 数字输入，filled text field 形制
- `.ctl-group` — 输入区控件分组

### 内联样式会压掉这些类

**内联样式的优先级高于类选择器。** 给元素挂了组件类之后再写内联的同名属性，类就失效了。这在本项目里已经造成过三次可见缺陷（见下一节第三条）。

需要动态改变的属性（如按钮的启用/禁用底色）应当在内联里写 `var(--md-sys-color-*)` 而非具体色值，这样仍然跟随主题。

---

## 五、改动时必须避开的陷阱

按发现代价排序——越靠前的越难在改动当时发现。

### 1. `innerText` 与 `textContent` 会抹掉内联 SVG

**症状**：图标在某次交互后永久消失，刷新才恢复。
**根因**：这两个属性只处理文本节点，赋值时会清掉所有子元素。
**做法**：任何可能含图标的元素一律用 `innerHTML`。特别注意「先存旧值、稍后恢复」的模式——`copyCodeBlock` 读取按钮文案存为变量、两秒后写回，**存取两端都必须是 `innerHTML`**，只改一端等于把图标丢在中途。

同类已修位置：`updateContextManagerPreview`、`smToggleArchived`、`toggleCodeBlock`、批注保存反馈。

### 2. `_expandStateMap` 依赖内联 `display`

**症状**：展开某个块后随便触发一次状态推送，内容就消失了，刷新又恢复。
**根因**：`chat.js` 的展开态保存循环用 `el.style.display === 'block'` 判断，读的是**内联值**。把 `display: none` 移进 CSS 类会让首次渲染的元素读不到 inline 值而永不被记录。
**做法**：可折叠内容的 `display` 保持内联，其余外观交给类。

### 3. 内联样式覆盖组件类

三次实例，症状各不相同但根因相同：

- `panels.js` 的 `setUIEnabled` 写死 `#007bff`，发送按钮永远是 Bootstrap 蓝
- `main.js` 的 `chatContainer.style.background` 写死 `#f5f5f5`，配色转向后聊天区显示为灰而非白
- `main.js` 的 `truncation-notice` 用 `cssText` 覆盖了同名 CSS 类，使新加的类完全不生效

**做法**：动态属性写令牌引用；静态外观全部交给类，不要在两处同时定义。

### 4. flex 项默认拒绝收缩

**症状**：按钮顶破容器；文本在两个字之后就被省略号截断。
**根因**：flex 项的 `min-width` 默认是 `auto`，等于内容的最小宽度。配上 `white-space: nowrap` 就意味着元素拒绝收缩到「文字加内边距」以下，于是挤走兄弟元素。
**做法**：需要收缩的 flex 项显式写 `min-width: 0`。`.md-button` 和 `.session-title` 都因此加了这一行。

### 5. 锚点默认可拖拽

**症状**：拖动完全没反应，控制台无任何报错。
**根因**：`<a>` 在 HTML 中天生可拖拽，父容器设 `draggable = true` 不覆盖它。抓着链接文字拖动时启动的是**链接拖拽**，`dataTransfer` 载荷是 URL 而非应用写入的 JSON，`JSON.parse` 抛异常后被空 `catch` 吞掉。
**做法**：可拖拽行内的锚点加 `draggable="false"`。

### 6. `order` 中点计算会自我抵消

**症状**：拖拽排序偶尔无效，或落到错误位置。
**根因**：取目标与邻居 `order` 的中点，前提是两者不相等。但批量创建的会话共享同一默认值，而每次拖拽都把相邻间距对半切，几次之后会触及浮点精度。此时中点等于已有值，后端写入成空操作，`sessionsHash` 不变，界面不动。
**做法**：见 `main.js` 的 `_dispatchReorder`——中点仅在严格落于两者之间时采用，否则把整个容器按目标顺序重新编号为 1..N，不依赖任何既有值。

### 7. 思维链卡片的 `th-open` 必须成对切换

**症状**：展开思维链后状态推送一次内容就没了。
**根因**：卡片用负 `margin-bottom` 塞在气泡背后，只露出顶部一截。展开时若不加 `th-open` 释放重叠，正文会渲染在气泡背后不可见。
**做法**：折叠处理器与 `_expandStateMap` 的恢复逻辑**都要**切换该类。漏掉恢复那一侧就是上面那个偶发症状。

### 8. 气泡不能加包裹元素

**根因**：`renderChat` 按 `chatContainer.children` 逐位置做 diff 替换，`minimap.js` 也遍历同一列表匹配 `msg-bubble-` 前缀。加一层 wrapper 会同时破坏这两处。
**做法**：思维链卡片是气泡的**兄弟节点**，靠绘制顺序落到后面——两者都是 `position: relative` 且 `z-index: auto`，同层叠上下文中 DOM 靠后的元素覆盖靠前的。

### 9. `dragleave` 从子元素冒泡

**症状**：拖拽指示线或高亮边框不停闪烁。
**根因**：指针从容器移到它的子元素时也会触发 `dragleave`。
**做法**：用 `if (!el.contains(e.relatedTarget))` 判断是否真的离开。

### 10. 拖影会连同半透明一起截图

**症状**：拖拽时看到两个半透明的重影。
**根因**：浏览器在 `dragstart` 同步阶段结束时对元素截图生成拖影，此时元素已经变半透明。
**做法**：添加 dragging 类推迟一帧（`requestAnimationFrame`）。

### 11. 选择器特异性压过组件类

**症状**：挂了 `.md-icon-button` 的按钮仍然带着旧的边框和底色。
**根因**：`.queue-ops button` 的特异性是 0,0,1,1，高于 `.md-icon-button` 的 0,0,1,0。
**做法**：容器级选择器只管布局（`display`、`gap`），外观交给组件类。

### 12. 同一概念的多份实现

本项目里若干视觉组件有两到三份独立实现，改一处不改其余会造成同一种内容在不同数据形态下长得不一样：

- Error/Result 状态标签 — 未补水内联、已补水内联、独立气泡**三份**
- 内联思维链 — `content_parts` 路径与 fallback 路径**两份**
- 分组头部与折叠符 — 侧边栏与会话管理弹窗**两份**
- 拒绝审批按钮 — 块头部与块下方独立按钮**两份**

改动前先 grep 确认有几处。补水前后会互相替换的那几对尤其明显，配色不一致会造成可见跳变。

### 13. 覆盖层的 Escape 由一个 document 级监听器统管

`utils.js` 末尾有一个 keydown 监听器负责全部覆盖层的 Escape 与 Tab 焦点循环，选择器是 `MD_OVERLAY_SELECTOR`。新增覆盖层只要带上其中任一 class 或 id 就自动获得这两个行为，不需要在 open 函数里接线。

**Escape 的实现是「点击该层自己的关闭控件」而非移除节点**（找 `.md-modal-close` / `.sm-close` / `[data-overlay-close]`）。这样 `closeSettingsModal` 之类既有的清理逻辑照原样执行，不会产生第二条会与它分叉的关闭路径。给自建覆盖层加一个带 `data-overlay-close` 的元素就够了。

**`showPromptModal` 与 `showConfirmModal` 刻意不在选择器里。** 它们的 Escape 必须 resolve 各自的 Promise，而这个监听器注册在前——抢先移除它们的节点会让 `await` 永远不返回。两者各自处理 Escape 并调 `preventDefault()`，而监听器开头检查 `e.defaultPrevented`，这是两侧唯一的协调机制。`showPromptModal` 原先不调 `preventDefault`，症状是一次按键关掉两层弹窗。

### 14. 跨 script 标签的 `var` 与 `let`/`const` 同名是 SyntaxError

**症状**：整个文件像是没加载，但 `typeof someFunction` 却返回 `'function'`。
**根因**：这个组合本身是解析期错误，文件不会被求值；而函数声明的提升发生在解析期，所以 `typeof` 仍然看得见它们。一旦真的调用，函数体里引用的任何 `let`/`const` 都还在 TDZ，抛 `ReferenceError`。
**诊断线索**：「只有 `typeof` 检查通过、所有实际调用失败」这个分布几乎只由这一种原因产生。同类的另一个成因是顶层求值在中途抛错（例如引用一个未加载文件里的标识符），两者症状完全相同。
**做法**：`main.js` 的 `socket` 因此是 `var` 加复用表达式而不是 `const io()`。给 harness 写桩时，main.js **自己声明**的名字（`postAction`）必须排在它之后，它**不声明**的名字（`socket`、`openEditModal`、`renameSession`）必须排在之前。

---

## 六、提交前的检查清单

按实际执行顺序排列。完整的验证命令见 `docs/HANDOVER.md` 第五节。

1. **推进 `frontend.html` 里对应的 `?v=N`。** 放在第一条是因为它是唯一一个会让所有其他检查都通过、而用户看到的仍是旧界面的项目。互相依赖的文件要同批推进。
2. **`node --check`** 改动过的每个 JS 文件。
3. **CSS 花括号收支归零**，并确认新引用的令牌都有定义。
4. **改过 `frontend.html` 就验证标签配对**，尤其是插入了内联 SVG 之后。
5. **新增了组件类就确认 CSS 与 JS 两侧都在。** 只有一侧存在的症状是元素完全没有样式，比改动前更难看。
6. **动态设置了背景色就确认同时设置了前景色。**
7. **暗色主题下扫一眼。** 入口在全局设置的「外观主题」，也可以直接给 `<html>` 加 `data-theme="dark"`。语法高亮已换整套 a11y-dark，代码块可读。这一步专门用来暴露第二节那个坑：只设了背景没设前景的地方，在亮色下往往因为默认深色文字而侥幸成立，暗色下才会现形。