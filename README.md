# 青灯书斋

自建小说站：FastAPI + SQLite，服务端渲染的阅读前台、txt 批量导入后台，
外加一套只读 JSON 接口和配套的开源阅读（Legado）书源。

分章器是这个项目的重点。实测 120 个真实 txt 里 70% 完全没有 `第X章` 标记，
所以"无章节标记的整本 txt"是主路径而不是边缘情况。

## 快速开始

```bash
pip install -r requirements.txt

cp .env.example .env        # Windows: copy .env.example .env
# 打开 .env 填上 ADMIN_PASSWORD 和 SECRET_KEY

python run.py                       # http://127.0.0.1:8000
# 或者
python -m uvicorn app.main:app --port 8000
```

首次启动会在 `data/` 下建 `novel.db`（WAL 模式）。打开 `/admin` 登录后到上传页丢 txt 进去。

## 配置

读取优先级：**系统环境变量 > 项目根目录的 `.env` > 内置默认值**。

`.env` 只支持最基本的写法：一行一个 `KEY=VALUE`，`#` 开头的整行是注释，
`export ` 前缀和成对的引号会被去掉。**不支持行尾注释**——密码里带 `#` 很常见，截断了更麻烦。
`.env` 已在 `.gitignore` 里；`.env.example` 是可以提交的模板。

想临时覆盖某一项，直接在命令行给环境变量就行，它会盖过 `.env`：

```bash
API_TOKEN=abc123 python run.py                     # bash
$env:API_TOKEN = "abc123"; python run.py           # PowerShell
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_PASSWORD` | 随机生成 | 后台密码。两处都没配就每次启动随机生成一个，打印在启动日志里 |
| `SECRET_KEY` | 随机生成 | 会话 cookie 签名密钥。不固定则重启即登出，日志会提醒 |
| `API_TOKEN` | 空 | 设置后 `/api/*` 需要 `?token=` 或 `Authorization` 头；留空则公开 |
| `SITE_BASE_URL` | `http://127.0.0.1:8000` | 写进书源与 API 返回 URL 的对外地址 |
| `SITE_NAME` / `SITE_TAGLINE` | 青灯书斋 / 一盏灯，一本书 | 站名与副标题 |
| `TG_API_ID` / `TG_API_HASH` | 空 | Telegram 频道爬取的应用凭证，见「Telegram 频道导入」一节 |
| `NOVEL_DATA_DIR` / `NOVEL_DB_PATH` | `data/` | 数据目录与库文件位置 |
| `NOVEL_ENV_FILE` | `.env` | 换一个配置文件路径。只能由系统环境变量指定 |
| `HOST` / `PORT` / `RELOAD` | 127.0.0.1 / 8000 / 关 | 仅 `run.py` 使用 |

## 部署

两条路：Docker（推荐，库文件挂在外面随时换）和裸机 systemd。两者都只跑**一个** uvicorn 进程——
SQLite 的写是串行的，多开 worker 换不来吞吐，只会让导入时互相等锁。

### Docker

```bash
cp .env.example .env
# 必改：ADMIN_PASSWORD、SECRET_KEY、SITE_BASE_URL=https://你的域名
mkdir -p data                 # 挂载点，让它归当前用户所有，容器里跑的是 uid 1000
docker compose up -d --build
docker compose logs -f web
```

镜像里没有 `.env`、没有库文件，`data/` 是唯一的持久化目录（`novel.db`、WAL、批量导入日志都在里面）。
容器内数据目录恒为 `/data`，`NOVEL_DATA_DIR` / `NOVEL_DB_PATH` 由 `docker-compose.yml` 接管，
写在 `.env` 里会被忽略。三个只有 compose 认识的变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DB_FILE` | `novel.db` | 用 `data/` 里的哪个库文件。**换库就是改这一项** |
| `HOST_PORT` | `8000` | 映射到宿主机的端口 |
| `BIND_ADDR` | `127.0.0.1` | 绑哪个地址。默认只对本机开，靠前面的 nginx 出去 |

> compose 解析 `.env` 比 `app/config.py` 严格：别写 `export ` 前缀，值也别加引号。

第一次起不来，九成是挂载目录的权限：容器里跑的是 uid 1000，而 docker 自动创建的目录、
或者用 root/scp 拷进来的库文件都归 root。两种报法都是同一个病：

- `unable to open database file`——目录不可写，库文件还没建起来。
- `attempt to write a readonly database`（挂在 `PRAGMA journal_mode=WAL` 上）——库文件存在但
  uid 1000 只读，SQLite 会静默降级成只读连接，直到第一次写才报错。

宿主机上 `sudo chown -R 1000:1000 data` 就好，注意要带 `-R`，`data/` 里的 `.db`、
`-wal`、`-shm` 都得一起换主。

### 换库

**停机替换**——最省心，适合小库：

```bash
docker compose stop web
cp /path/新库.db data/novel.db
rm -f data/novel.db-wal data/novel.db-shm   # 必须删，旧 WAL 配新库会读出错乱内容
docker compose start web
```

**改名切换**——几乎不停机，几十 GB 的库建议走这条：

```bash
scp 新库.db 服务器:/srv/novel/data/novel-20260901.db   # 老库照常服务
# 然后在 .env 里改 DB_FILE=novel-20260901.db
docker compose up -d          # 重建容器，切换是秒级的；回滚就把 DB_FILE 改回去
```

想从**正在跑**的站点导一份一致的快照，别直接 `cp`（WAL 里还有没落盘的事务），用 `VACUUM INTO`：

```bash
docker compose exec web python -c "import sqlite3;sqlite3.connect('/data/novel.db').execute(\"VACUUM INTO '/data/snapshot.db'\")"
```

> 一个坑：Windows 上的 Docker Desktop 别把 `./data` 落在 Windows 盘（`C:\` `E:\`）上。
> SQLite 的 WAL 依赖共享内存，隔着 virtiofs/9p 会报 `disk I/O error` 或一直 locked。
> 要么把项目放进 WSL2 的文件系统，要么把 `./data:/data` 换成 docker 命名卷。Linux 服务器上没这问题。

### 批量导入与更新

```bash
# 灌本地 txt：先在 docker-compose.yml 里放开 /srv/txt:/txt:ro 那行挂载
docker compose exec web python scripts/bulk_import.py /txt -r --workers 4

# 拉了新代码
docker compose up -d --build        # 库在挂载卷里，重建容器不影响数据
```

### nginx 与 HTTPS

```nginx
server {
    listen 443 ssl;
    server_name 你的域名;
    # ssl_certificate / ssl_certificate_key 交给 certbot

    client_max_body_size 600m;      # ZIP 上限 512MB，留点余量
    proxy_read_timeout 300s;        # 大文件上传别被掐断

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

公网部署请务必挂 HTTPS：会话 cookie 只有 `HttpOnly` + `SameSite=Lax`，**没有 `Secure` 标记**，
明文 HTTP 下管理员密码和 cookie 都是裸奔。另外 `/admin/login` 没有失败次数限制、全站没有限流，
密码要够长；只想给自己用的话，把后台锁在内网或加一层 nginx basic auth 更稳。

探活打 `GET /healthz`，返回 `{"ok": true, "books": N}`，只数一次 `books` 表，不碰 FTS。
容器的 `HEALTHCHECK` 用的就是它。

### 裸机 systemd

```ini
# /etc/systemd/system/novel.service
[Unit]
Description=青灯书斋
After=network.target

[Service]
User=novel
WorkingDirectory=/srv/novel
Environment=PYTHONIOENCODING=utf-8
ExecStart=/srv/novel/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory` 下要有 `.env`（`app/config.py` 按项目根目录找），库默认落在 `data/`，
想放到别处就在 `.env` 里给 `NOVEL_DB_PATH` 绝对路径。上线前确认这台机器的 SQLite 够新
（全文检索要 FTS5 + trigram，即 3.34+）：

```bash
python -c "import sqlite3;sqlite3.connect(':memory:').execute(\"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')\");print(sqlite3.sqlite_version, 'ok')"
```

## 目录结构

```
app/
  main.py            FastAPI 装配、静态与模板挂载、错误页
  config.py          路径、密钥、可选开关
  db.py              sqlite3 helper（每线程一连接、WAL、外键）
  schema.sql         books / chapters / chapters_fts / import_jobs / app_settings / tg_channels
  security.py        管理员口令校验与签名 cookie
  paging.py          分页计算
  templating.py      Jinja2 环境、过滤器、占位封面配色
  services/
    decoder.py       编码探测与解码
    txt_parser.py    章节识别 + 分节 + 段落格式化（核心）
    importer.py      单本/多文件/ZIP 导入、去重、任务进度、重新分章
    telegram.py      TG 频道爬取：网页登录、频道管理、增量同步、自动调度
    settings.py      可持久化的解析选项
    repo.py          书籍与章节查询、FTS 检索
    booksource.py    Legado 书源生成
  routers/
    site.py          前台页面
    admin.py         后台页面与操作接口
    tg.py            后台的 TG 频道页
    api.py           阅读软件用 JSON 接口
templates/  static/  legado/  data/（已 gitignore）
Dockerfile  docker-compose.yml  .dockerignore   部署用，见「部署」一节
```

## 分章器

解码 → 规范化 → 识别开头 `######` 标题块 → 扫描章节标记 → 三态分章 → 段落格式化。

### 三种模式

| 模式 | 触发条件 | 结果 |
| --- | --- | --- |
| `marked` | 命中 ≥ 2 个标记，且通过合理性校验 | 按标题行切分 |
| `single` | 只有 1 个标记且落在开头，或正文短于分节字数 | 整本一章 |
| `auto` | 无可信标记且正文较长 | 按目标字数在段落边界切节 |

标记要求整行、去空白后不超过 40 字：`第X章/节/回/卷/篇/集/话/幕`、
`序章/楔子/引子/前言/后记/尾声/终章/番外`、`Chapter N`；
`1.` 这类编号列表与 `（一）` 属弱标记，同一样式命中 3 次以上才启用，否则会把编号清单当章节。

标记合理性校验有两条，都来自实测：

- 相邻标记之间正文不足 200 字、且这种情况占比过半 → 判定误命中（目录页、人物列表）
- 首个标记之前的正文超过全文一半 → 标记不是章节结构（有整本 4 万字只在末尾冒出两个标记的文件），退回按字数分节

### auto 模式的分节

贪心累加段落，达到目标字数（默认 3000，后台可调）就断章，每节都在段落边界结束。三个针对实测数据的处理：

- **超长单段**：单段超过目标 1.5 倍（实测最长 6737 字）时在段内句末标点 `。！？…` 处二次切分，切点取最接近目标的位置；找不到标点就按字数硬切
- **避免双倍章**：已攒到目标 80% 且再加一段会超过目标 1.3 倍时提前断章
- **尾章过短**：末节不足目标 30% 时并入上一节

章名为 `第 N 节`，可附带该节首段的第一个短句作副标题（`第 3 节 · 清早，路至诚打了个哈欠`），纯装饰，后台可关。

`marked` 模式下首个标记之前如果有正文，会单独成"开篇"；超过分节字数时按 auto 规则切成 `开篇 第 N 节`。

### 段落格式化

- 丢弃纯空白行与分隔线（`====`、`----`、`***`、`######`）
- 广告行黑名单（网址、`更新最快`、`请收藏`、`本书由…整理`、群号等），命中且整行短于 60 字才删
- 合并被硬换行截断的句子：上一行不以句末标点收尾、下一行也没有缩进时接续
- 正文不存全角缩进空格，首行缩进交给 CSS `text-indent: 2em`

### 解析设置（后台可改，存数据库）

- **分节字数**：默认 3000
- **剥离开头 `######` 标题块**：默认关闭。关闭时正文第一段会是那行 `#  书名`，阅读页会比较难看；
  120 个样本文件全都有这个块，**建议打开，然后到书籍列表做一次批量重新分章**
- **把"幺"改回"么"**：默认关闭，实测 118/120 个文件受影响
- **分节副标题**：默认开启

书名抽取始终执行，与是否剥离标题块无关：优先读标题块内容，其次解析文件名里的
`《书名》作者：xxx` / `书名-作者`，否则取文件名主干。旧目录里文件名是 URL 编码的中文，导入时先 `unquote`。

## 后台

`/admin`，单一管理员密码，会话是 `itsdangerous` 签名的 HttpOnly + SameSite=Lax cookie。

- **上传**：拖拽区一次多选 txt，也支持 ZIP（校验路径穿越、单条与总解压体积、条目数上限，只取 `.txt`）。
  表单有三个即时选项：分节字数、强制整本单章、强制按字数切。上传入队后台任务，
  前端轮询 `/admin/api/jobs/{id}`，逐本显示分章模式与章数——分章异常的书一眼能看出来
- **TG 频道**：登录 Telegram 账号后，把公开频道里的 txt 逐本爬进书库，见下节
- **书籍管理**：分页列表，可按分章模式筛选；编辑书名/作者/简介/封面/分类；删除（连带章节与索引）
- **重新分章**：单本，或勾选若干本，或对当前筛选条件全量重跑。用库里现存正文重新解析，
  书名作者这些人工改过的字段不受影响。调完设置后批量修复就靠它
- **章节**：列表分页，单章在线编辑正文
- **解析设置**：上面那几个开关

内容去重按"去掉全部空白的正文"取 sha256，同一本书换编码、换换行风格不会重复入库。

## Telegram 频道导入

后台「TG 频道」页可以把公开频道里的 txt 附件一本一本爬进书库，前台实时可见。

### 一次性准备

1. 在 [my.telegram.org](https://my.telegram.org) → API development tools 创建应用，
   把拿到的 `TG_API_ID` / `TG_API_HASH` 写进 `.env`（Docker 部署同样写在 `.env`，
   compose 会透传进容器），重启服务
2. 打开 `/admin/telegram`，输手机号（带国家区号）→ 收验证码 → 输码；开了两步验证的再输云密码。
   登录态（会话字符串）存在数据库里，重启不用重登

### 使用

- 粘贴 `t.me/xxx` 这样的公开频道链接添加频道（私有频道的 `+` 邀请链接不支持）
- 「同步」逐个下载频道里的 txt 入库；图片、视频、epub 等非 txt 附件直接忽略，
  超过 64MB 的 txt 记为失败不下载。同步在后台线程跑，不挡网站；同一时刻只允许一个同步任务
- 同步只处理上次之后的新消息（按消息 id 增量），已入库的内容再有 `content_hash` 判重兜底，
  所以重复点同步是安全的
- 「自动同步」设个间隔（分钟，0 关闭），到了点就把所有启用频道各同步一遍；
  重启服务后从上次触发时间继续算
- 进度复用导入任务的轮询页面，每本一行结果；中断的同步下次接着断点续爬

> 登录的是你自己的 TG 账号，只用来读频道消息。`tg_session` 等状态存在库里，
> 换库（`DB_FILE`）后要重新登录； TG 频道列表存在 `tg_channels` 表里，同样跟着库走。

## 前台

- 首页：书架网格（无封面时按书名生成稳定的渐变占位块）、最长篇幅榜、搜索框
- 书籍页：书籍信息 + 分页目录；单章书隐藏目录，直接"开始阅读"
- 阅读页：单章一屏，键盘 ← → 翻章，字号 / 行距 / 主题（纸黄、纯白、夜间）与阅读位置存 `localStorage`；
  单章书隐藏翻章按钮
- 搜索页：书名模式走 LIKE，全文模式走 FTS5 并高亮片段

全文检索用 FTS5 external content 表（`content='chapters'`）+ `tokenize='trigram'`：
正文只存一份，中文子串检索可用且无需分词器。关键词短于 3 字时 trigram 用不上，会退回 LIKE 逐字匹配。

## 书源接口与开源阅读

四个只读接口，统一返回 `{"isSuccess": true, "data": ...}`：

| 接口 | 说明 |
| --- | --- |
| `GET /api/search?key=&page=` | 书名与作者模糊匹配 |
| `GET /api/book/{id}` | 详情，含最新章节 |
| `GET /api/book/{id}/catalog?page=` | 目录（`id`、`title`、`url`，多页时给 `nextUrl`） |
| `GET /api/chapter/{id}` | 章节正文，段落以 `\n` 分隔纯文本 |

单章书走同一路径，目录返回单元素数组，Legado 能正常处理。接口文档在 `/api/docs`。

### 导入书源

两种方式，二选一：

1. **网络导入（推荐）**：开源阅读 → 书源管理 → 右上角 → 网络导入，地址填
   `http://你的地址:8000/legado/源.json`。这个地址按服务端当前的 `SITE_BASE_URL`
   和 `API_TOKEN` 实时生成，不用手改
2. **本地导入**：把 `legado/源.json` 拷到手机，书源管理 → 本地导入。
   这份静态副本里的地址是 `http://127.0.0.1:8000`，**必须把文件里所有这个地址替换成实际可访问的地址**
   （手机访问电脑要用局域网 IP，例如 `http://192.168.1.10:8000`）；
   如果设了 `API_TOKEN`，四个 url 后面都要带上同一个 `&token=...`

> `/api/*` 默认无鉴权，因为书源抓取需要如此。介意的话设 `API_TOKEN`，
> 书源 URL 带同一个 token。上传与后台一律要求管理员会话。
> 另外这套服务没做限流，别直接暴露在公网上。

## 实测结果

120 个真实 txt 全量导入：120 成功、0 失败，2.6 秒，库 38 MB，953 个章节、321 万字。

- 6 个小于 5KB 的文件全部判为 `single`，各 1 章
- 86 个没有 `第X章` 标记的文件里 73 个走 `auto`、9 个 `single`，4 个靠 `Chapter N` 之类的其他标记走了 `marked`
- `auto` 书平均每节 2997 字，最长的书平均每节 3551 字
- 剩下的超长章节都是原作者自己的长章（最长 26213 字的 `第32章`），`marked` 模式按设计尊重原有标记

## epub 预留

`txt_parser.py` 输出统一的 `ParsedBook(title, author, chapters, split_mode)`，
`importer.import_one` 按扩展名分派，本轮只有 txt 分支。需要 epub 时装 `ebooklib`
写一个返回同样 `ParsedBook` 的解析函数接进 `import_one` 即可，其余代码不用动。

## 几点取舍

- `SECRET_KEY` 不设就每次启动随机生成，等于重启即登出。长期跑请写进 `.env` 固定下来
- `.env` 的解析是手写的十几行，只覆盖常见写法：没有多行值、变量插值、行尾注释。
  这样省掉一个依赖，代价是别在里面写花活
- 上传的原始文件不落盘，正文只存在数据库里；`重新分章` 用的是库里的正文，
  所以段落格式化和广告行清理这两步是不可逆的
- `strip_header_block` 与 `fix_yao_variant` 按方案定为默认关闭。真要用建议打开后批量重新分章一次
