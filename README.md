# 原型易用性測試（獨立網站版）

上傳 HTML 原型，AI 產生測試任務；受測者打開連結實際操作後，整理出成功率、卡關位置與改善建議。

不需要 Claude 帳號就能使用：管理者用密碼登入，受測者只要打開連結，不用登入也不用安裝。

## 檔案

| 檔案 | 用途 |
| --- | --- |
| `server.py` | 網站後端：登入、儲存原型與結果、呼叫 AI |
| `ai.py` | AI 串接，可切換 Claude API 或 OpenAI 相容服務 |
| `static/index.html` | 前端畫面（管理介面與受測畫面） |
| `Dockerfile` | 部署用 |
| `.env.example` | 設定範本 |

資料存在 SQLite 資料庫（`data/app.db`），不需要另外架資料庫。

## 設定

複製 `.env.example` 成 `.env`，填入：

- `ADMIN_PASSWORD`：管理者密碼
- `SECRET_KEY`：一串隨機長字串，用來保護登入狀態
- `AI_PROVIDER`、`AI_API_KEY`、`AI_MODEL`：AI 服務設定

AI 預設使用 Claude API（`AI_PROVIDER=anthropic`，模型預設 `claude-sonnet-4-6`）。要改用 OpenAI 或其他相容 OpenAI 格式的服務，把 `AI_PROVIDER` 設為 `openai`，並填 `AI_BASE_URL` 與 `AI_MODEL`。不設定 AI 也能使用，只是任務要手動建立、沒有 AI 整理發現。

AI 只在「產生任務」和「整理發現」時使用，受測者做測試不會用到。費用依各服務商計價，單次通常很低。

## 在自己電腦上試用

```bash
pip install -r requirements.txt
export ADMIN_PASSWORD=你的密碼 AI_API_KEY=你的金鑰    # Windows 用 set
python server.py
```

打開 http://127.0.0.1:8080 。這樣只有你自己的電腦連得到，外部受測者無法使用。

## 部署到網路上

外部受測者要能打開，網站必須放在有公開網址的地方。

**公司伺服器（Docker）**

```bash
docker build -t usability-test .
docker run -d -p 8080:8080 -v usability-data:/data --env-file .env usability-test
```

請 IT 設定 HTTPS 網址指向這台伺服器的 8080 埠，並在 `.env` 設 `COOKIE_SECURE=1`。

**雲端平台**

可以選擇支援 Docker、可從 GitHub 自動部署的雲端平台。重點有兩個：

1. 環境變數照 `.env.example` 設定在平台的後台，不要把 `.env` 上傳到 GitHub
2. 一定要掛載「持久化磁碟」到 `/data`，否則每次重新部署，測試資料會全部消失

## 使用流程

1. 用密碼登入，在「設定任務」上傳 HTML 原型，AI 會產生 3–5 個任務
2. 每個任務按「試做」，確認出現「偵測到完成」
3. 到「邀請受測者」複製測試連結，傳給同事或餐廳夥伴；現場測試就用平板打開同一個連結
4. 在「測試結果」看每個任務的成功率、時間、誤點與意見，按「請 AI 整理發現」取得問題與建議

## 注意事項

- 原型要是單一 HTML 檔（CSS、JavaScript 內嵌），最大 5 MB
- 拿到測試連結的人都能看到原型內容，敏感的設計請注意連結的分享範圍
- 原型在受限的沙盒中執行，無法讀取網站的登入資訊；原型中連到外部網站的連結會被停用
- AI 產生的完成條件不一定都正確，正式測試前每題都要試做確認
