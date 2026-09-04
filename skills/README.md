# Skills

給 Claude Code（或其他支援 Agent Skills 的 client）用的技能包。它們不是 MCP server 的一
部分，也不會隨 `pip install mcp104` 安裝——MCP server 提供的是「能對 104 做什麼」，這裡
放的是「拿這些工具做一件完整的事該怎麼做」。

## 安裝

### 方式一：裝成 plugin（建議）

這個 repo 同時是一個 Claude Code marketplace 和它自己的 plugin，所以兩行就好：

```
/plugin marketplace add jasoncychueh/104mcp
/plugin install mcp104@104mcp
```

裝完會同時拿到 **skill** 和 **MCP server 的設定**（`.claude-plugin/plugin.json` 裡宣告了
`uvx mcp104` 這個 stdio server），不必自己編 `.mcp.json`。更新用 `/plugin marketplace update 104mcp`。

前提是機器上有 `uv`／`uvx`；`uvx` 會在第一次啟動時自己去 PyPI 取 `mcp104`。

### 方式二：只要 skill，手動複製

不想裝 plugin、或只想要 skill 不想要 MCP 設定的話：

```bash
cp -r skills/104-candidate-screening ~/.claude/skills/          # 個人層級
cp -r skills/104-candidate-screening <你的專案>/.claude/skills/  # 專案層級
```

Windows（PowerShell）：

```powershell
Copy-Item -Recurse skills\104-candidate-screening $HOME\.claude\skills\
```

兩種方式裝好之後，Claude 都會自己在相關的請求上叫用它，不需要手動觸發。

## 目前有什麼

### `104-candidate-screening`

替一個職缺找人、篩人，最後產生一份可離線閱讀的 HTML 履歷報表（含照片、附件清單、完整
履歷原文、分組與篩選）。

涵蓋的東西：

- **配額安全的呼叫順序**——只有 `get_resume_detail` 會扣 104 每日 300 筆的額度，清單類
  呼叫不扣。整個流程的形狀是被這件事決定的。
- **履歷記錄的 JSON 格式**與分批寫檔的做法。
- **怎麼把用人主管的「我要什麼樣的人」變成可重現的分組與硬排除。**
- **`scripts/build_report.py`**——設定檔驅動的報表產生器，照片內嵌成 base64、單檔自足。

需要 Python 3 與 Pillow（只有處理照片時需要）。

發訊息（`send_inquiry` / `send_message`）刻意不在範圍內：送出去收不回來、沒有 dry-run，
該由人決定。
