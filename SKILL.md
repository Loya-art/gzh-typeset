---
name: gzh-typeset
description: "公众号排版流水线（秀米自动化，自用不对外分享）。把「写好的公众号推文 + 一批照片」变成「秀米里排好版、可出预览二维码」的图文：套秀米风格模板 或 自建 HTML 排版 → 上传图库 → 替换文案/图片 → 按推文要求调格式（微软雅黑/14号/行距1.5/首行缩进/两端对齐）→ 保存 → 出预览二维码与预览长图。当用户说「帮我排公众号」「把这篇文章排进秀米」「套个模板排版」「排好给我一张预览二维码」「公众号推文排版」等时触发。本 skill 只做排版与自动化，不负责发布群发。"
metadata:
  type: operator
  version: "1.0.0"
  tags: [wechat, gzh, xiumi, typesetting, playwright, automation]
---

# 公众号排版流水线（gzh-typeset）

**这个 skill 是给 AI 自己用的操作手册 + 工具箱**，不是分享包。
目标：同类任务**第二次起更快、更准**——脚本固化动作，`learned/` 固化经验。

## 0. 三层结构

| 层 | 位置 | 作用 | 谁维护 |
|---|---|---|---|
| 工具 | `scripts/gzh.py` | 秀米自动化 CLI（起浏览器/上传/套模板/改文案换图/调格式/保存/出二维码） | AI（发现流程变了就改它） |
| 经验 | `learned/` | 会**越长越厚**的知识：公众号档案、选择器与流程、运行日志 | **AI 每次跑完必须回写** |
| 依赖 | `skills/gzh-content-skill/` | 写文案/自建 HTML 排版的第三方 skill（git 仓库，别改） | 上游 |

## 1. 开工前（必做，30 秒）

```bash
cd <本 skill 目录>
cat learned/公众号档案.md      # 这个号要什么格式、页尾长什么样、常用哪套模板
cat learned/选择器与流程.md    # 已实测的选择器/入口，别重新摸索
tail -40 learned/运行日志.md   # 上次踩了什么坑
python scripts/gzh.py status   # 浏览器在不在、登没登录、当前编辑的是哪篇
```

`status` 里 `logged_in: false` → 执行 `python scripts/gzh.py start`，**浏览器窗口会弹出来给用户看**，
让用户扫码/登录（登录态存在 `~/.gzh-content/xiumi_profile`，能撑很久）。

## 2. 两条排版路线（选一条）

### 路线 A：套秀米模板（用户偏好，好看）
```bash
python scripts/gzh.py shop --keyword 军训 --free        # 商城搜免费模板，拿到卡片 tag
python scripts/gzh.py shop-save --tag 开学新生军训       # hover 卡片 → 预览 → 另存给自己
python scripts/gzh.py open --match 军训夏令营            # 「打开」弹窗里选它并打开
python scripts/gzh.py cells                             # 看清模板有哪些文本单元格
python scripts/gzh.py set-cells map.json                # 按序号把示例文案换成我们的
python scripts/gzh.py set-images urls.json              # 替换图片位（按文档顺序）
python scripts/gzh.py add-images tail.json              # 图片位不够时克隆补图
python scripts/gzh.py format --cells 5,12,15 --size 14 --line-height 1.5
```
> 模板文案**必须全部替换**（秀米提示：不换会撞公众号原创保护）。

### 路线 B：自建 HTML 排版（要求极精确时）
```bash
# 用 gzh-content-skill 的 inject_layout.py / 自己的生成脚本产出 layout.html
python scripts/gzh.py title "标题"
python scripts/gzh.py inject layout.html
```

### 两条路线都要先做：图片进图库
```bash
python scripts/gzh.py upload img/01.jpg img/02.jpg ...
python scripts/gzh.py lib -o library.json    # 名字 -> CDN 地址，写 HTML 时直接用
```

## 3. 收尾：预览二维码

```bash
python scripts/gzh.py preview -o <输出目录>
# 产出：预览二维码.png（临时码）/ 排版预览长图.png / 编辑器截图.png / 预览链接
```
- **预览二维码是临时的**（秀米明说「短期内失效」），要给组长看就尽快发。
- 新注册账号会提示「尚未审核，分享二维码和链接还不可用」——长期分享链接要等审核。

## 4. 收工后（必做：让 skill 变聪明）

1. `learned/运行日志.md` 追加一行：日期 / 任务 / 路线 / 结果 / 新坑。
2. 选择器或入口变了 → 改 `learned/选择器与流程.md`，并同步改 `scripts/gzh.py`。
3. 用户提了新要求（字号/页尾/模板偏好/固定文案）→ 写进 `learned/公众号档案.md`。
4. 同一类操作做过 3 次以上 → 把它**固化成一个新子命令**写进 `scripts/gzh.py`，并在运行日志里注明。

## 5. 已知坑（实测，别重踩）

- **本机 Playwright 1.61 要 `chromium-1228`，实际装的是 `chromium-1234`** → 已用目录 junction 指向 1234；
  换机器/升级后若报 `Executable doesn't exist`，照此再 junction 一次。
- 秀米**登录检测别用「我的秀米」文字**（未登录首页也有），要看右上角**可见的「登录」链接消失**且 URL 不含 `/auth`。
- `/auth` 页面会**自动跳微信扫码登录**（`open.weixin.qq.com/connect/qrconnect...`）；用 WeChat 扫码最快。
- **浏览器必须常驻可见**：用 CDP（9222）+ 持久化 profile；脚本一律 `connect_over_cdp` 接同一个窗口，
  不要每步新开浏览器（用户会以为「浏览器压根没动」）。
- 秀米编辑器正文是**一堆 `div[contenteditable=true]` 单元格**（`.tn-cell-inst`），图片是 `.tn-image-inst-wrapper`；
  保存是**从 DOM 取内容**，所以直接改 DOM + 派发 `input` 事件能存住（已实测）。
- 商城模板卡片 = `div.entry.thumbnail`，卡片标题在图里、只能靠**标签文字**定位；
  套模板 = hover → `.my-show-mask` 里的「预览」→「另存给自己」。
- 图文「打开」是**弹窗流程**：点顶部「打开」→ 弹窗里点卡片 → 再点弹窗底部绿色「打开」。
