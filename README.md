# steam-crawl

用于市场调研的公开 Steam 游戏数据采集 CLI，Python 3.10+，仅使用标准库。
官方接口优先，按来源保留原始字段与时间戳，不采集用户资料、登录态或私有数据。

## 数据来源与边界

| 来源 | 内容 | 默认策略 |
| --- | --- | --- |
| Steam Web API | `IStoreService/GetAppList/v1` 游戏目录、`ISteamUserStats/GetNumberOfCurrentPlayers/v1` 当前在线人数 | 目录需要 API key；同主机请求间隔至少 2 秒，在线人数缓存 5 分钟 |
| Steam Store | 官方商店 `api/appdetails` 的名称、开发商、发行商、类型、发行日期、平台、地区价格等 | 每次一个 appid，间隔至少 2 秒，缓存 24 小时，按国家和语言隔离 |
| SteamSpy | `appdetails` 的拥有者估算区间、标签和游玩时长等 | 显式 `--steamspy` 启用，间隔至少 1 秒，缓存 24 小时 |
| SteamDB | 已获许可的本地补充维度 | **不联网抓取**；必须确认使用权限，附来源 URL 和采集时间 |

Steam Store 的 `appdetails` 是未承诺稳定性的公开商店接口，不等同于有稳定性保证的
Steamworks 文档接口。SteamDB 无公开 API，且其 FAQ 限制自动抓取，因此这里不实现
前端接口逆向、验证码绕过、代理轮换或伪装浏览器；只有取得授权的数据才可本地导入。
如果业务必须使用 SteamDB 在线接口，应先取得其明确许可和接口规范，再扩展适配器。
公开可访问不代表拥有无限制采集、商业使用或再分发权限，本工具不构成法律合规保证。

使用前核对最新条款：[Steam API 条款](https://steamcommunity.com/dev/apiterms/)、
[Steam 官方目录接口](https://partner.steamgames.com/doc/webapi/IStoreService)、
[SteamSpy API](https://steamspy.com/api.php)、[SteamDB FAQ](https://steamdb.info/faq/)。
当前开发环境无法访问这些站点，因此没有通过在线访问验证最新条款或真实响应。
请求间隔是保守的本地默认值，不是服务方对访问权限或额度的保证。

## 使用

在仓库根目录运行，不需要安装依赖：

```sh
python -m steam_crawl --help

# 官方目录：key 仅从环境变量读取，不放在命令行、日志或数据库
# 通过运行环境的 secret 管理器注入 STEAM_API_KEY
python -m steam_crawl catalog --max-pages 1 > /tmp/catalog.jsonl

# 显式指定公开游戏 appid，无需目录 API key；SteamSpy 可选
python -m steam_crawl crawl 570 730
python -m steam_crawl crawl 570 730 --country cn --language schinese --steamspy

# JSONL 导出，默认数据库 data/steam.sqlite3 已被 .gitignore 忽略
python -m steam_crawl export > /tmp/games.jsonl
python -m steam_crawl --db /tmp/research.sqlite3 --timeout 15 --retries 1 crawl 570
```

全局参数放在子命令前面。`catalog` 每行输出一页，包含 `apps`、`have_more_results` 和
`last_appid`；有更多数据时，用 `catalog --last-appid <游标> --max-pages 1` 续取。
默认只请求一页、仅包含游戏，不自动遍历全站，不回退到已弃用的无密钥目录接口。
从目录选出需要的 appid 后再运行 `crawl`，避免没有研究目的的全量重复采集。
目录页只输出到 stdout，不写入游戏快照或缓存；重复目录查询也会消耗额度。

授权 SteamDB 本地文件格式（JSON 数组）：

```json
[
  {
    "appid": 570,
    "source_url": "https://steamdb.info/app/570/",
    "collected_at": "2026-10-01T00:00:00Z",
    "data": {"example_authorized_dimension": 123}
  }
]
```

`data` 中只填写你有权使用的真实补充维度；上例不是实际统计数据。
`collected_at` 必须为带时区的 ISO 8601 时间。`--authorized` 是操作者的权限确认，
不授予数据许可。导入无需网络，且不会覆盖官方或 SteamSpy 字段：

```sh
python -m steam_crawl import-steamdb /tmp/authorized-steamdb.json --authorized
```

## 数据语义与可靠性

- SQLite WAL 持久化每次成功快照，逐来源提交；重跑优先使用有效缓存，源之间独立降级。
  失败不覆盖此前成功数据；导出中 `fetched_at` 是本地获取时间（UTC Unix 秒），分析时必须
  检查其新鲜度。SteamDB 还保留上游采集时间 `collected_at`，本地导入时间不代表数据刷新。
- 导出每个 appid 的各来源、各地区/语言最新快照，路径为
  `sources.<来源>.<scope>.{fetched_at,data}`。商店 scope 如 `us:english`，
  其余来源为 `global`；历史仍保留在 `observations` 表中。
- SteamSpy `owners_estimate_range` 是**拥有者估算区间，不是实际销量**，可能包含免费游戏、
  赠送或激活用户，不能直接作为付费销量或收入。`is_estimate` 固定为 true，并保留 `raw`。
  缺失价格、不可用游戏、被下架或地区受限不自动补零；在线人数也不等于拥有者。
- 请求有超时、响应大小上限和最多 2 次默认重试（可设 0–5），只对网络故障、429 和选定
  5xx 做有限指数退避。遵守 `Retry-After` 秒数/日期；等待超过 60 秒时停止该来源本轮访问，
  不提前重试。401/403 或持续 429 会暂停同一服务组本轮后续访问，不绕过访问控制。
- `crawl` 单个来源失败仍处理其余来源和 appid，stderr 输出无密钥错误摘要，
  stdout 每行输出 appid 与错误列表；任何采集失败返回退出码 1。缓存命中和全部成功返回 0。
  进程终止后可用相同命令重跑，已提交数据不会丢失。目录分页中断时从最后成功游标续取。
- 这是单进程、串行采集器，不是分布式调度服务。不要并行启动多个 worker 绕过限速；
  生产部署应由单实例调度器限制每日总请求（Steam Web API 条款的每日上限也需核对），
  监控非零退出码，收到拒绝访问后先人工检查许可和服务状态，不立即循环重启。
  本地缓存和暂停状态不提供跨进程共享限流；暂停状态仅在当前进程生效。
- 不自动读取 `.env`，不保存 API key；数据库、授权导入文件及导出文件应按本地数据安全要求
  控制权限和备份。不要提交真实密钥或数据；将导出的上游文本用于网页时仍需转义。

## 测试

```sh
python -m unittest discover -s tests -v
```

测试只使用标准库、临时数据库和模拟 HTTP，不需要 API key 或外网。
覆盖访问暂停与重试、缓存/地区隔离、失败保留、目录游标、估算标识、授权导入和 CLI。