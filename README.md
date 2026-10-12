# law_crawl —— 法律、法规、规章采集

定时采集法律、法规和部门规章的目录与正文，由 GitHub Actions 运行，数据发布在本仓库的 Release 里。

## 数据来源

| 来源 | 内容 | 取数方式 |
| --- | --- | --- |
| 国家法律法规数据库（flk.npc.gov.cn） | 宪法、法律、行政法规、监察法规、司法解释、地方性法规，约 3.1 万部 | 目录接口全量比对；正文取官方 docx |
| 生态环境部、交通运输部、应急管理部 | 各部规章库的现行有效规章 | 分页静态列表；正文取 docx 或详情网页 |
| 住房和城乡建设部、市场监管总局 | 各部规章库的现行有效规章 | 栏目页面调用的内容接口；正文取 docx 或详情网页 |
| 国家能源局 | “能源法律法规专栏”里的规章及规范性文件 | 专栏页面；没有公布日期和时效性，状态标为“时效待核实” |

未收录：其他部门的规章、地方政府规章。

## 数据文件

发布在 Release `data` 下，每次采集后覆盖更新：

- `laws.db`：法规目录（SQLite）。`laws` 表每部法规一行（标题、类别、制定机关、地区、公布与施行日期、时效性、
  来源 `source`、官网地址 `source_url`、是否仍被来源收录 `is_listed`）；`law_changes` 表记录历次发现的变更
  （新增、时效性变化、不再收录、重新收录）；`meta` 表是快照日期等说明。
- `laws_content.db`：法规正文（SQLite）。`contents` 表每部法规一行，`body` 是 zlib 压缩的段落文本（换行分隔）；
  `unreadable` 表记录正文文件无法解析的法规（如官方只提供旧版 doc）。

仓库里的 `manifest.json` 是最近一次采集后的数据清单（快照日期、各来源条数、已有正文的部数、最近的变更）。

下载地址：

```
https://github.com/martian2035-dev/law_crawl/releases/download/data/laws.db
https://github.com/martian2035-dev/law_crawl/releases/download/data/laws_content.db
```

## 流水线

- `.github/workflows/crawl.yml`：北京时间每天 02:30 运行，也可在 Actions 页面手动触发。流程：取回已发布的数据 →
  检查各数据源能否访问 → 目录比对、取缺的正文、同步部门规章 → 发布数据 → 回写 `manifest.json`。
  一次运行最长约 4.5 小时，切成 45 分钟一段，每段结束发布一次数据，作业中途被取消最多丢一段的进度。
  正文没取完的下次接着取；全部取完后每次只处理新增的法规。
- `.github/workflows/test.yml`：代码变更时运行测试。

复制本仓库另行部署时：

1. 在 GitHub 建一个公开仓库并推送本目录（工作流自带发布 Release、回写清单所需的 `contents: write` 权限）；
2. 在 Actions 页面手动触发一次“采集法规与规章”，看“检查各数据源能否访问”这一步的输出。

已经在本地取过一部分正文的，可以先把它传上去，流水线会在此基础上继续：

```bash
gh release create data --title "法规数据" --notes "由流水线维护的数据文件" 2>/dev/null
gh release upload data /path/to/laws.db /path/to/laws_content.db --clobber
```

### 关于访问限制

- 国家法律法规数据库在访问频繁时会返回浏览器验证页并拒绝服务。采集遇到验证页立即停止，当天不再继续，
  不重试也不绕过。请求间隔（目录 1 秒、正文 2 秒）和“同一时刻只跑一个采集”都是为了不触发它，不要调高频率。
- GitHub 托管的运行器在境外。若“检查各数据源能否访问”显示某个站点不可访问，说明该站点不对境外开放，
  这时可以改用自托管运行器（一台能访问这些站点的机器）：把 `crawl.yml` 里的 `runs-on` 改成 `self-hosted`。

## 本地运行

需要 Python 3.12 及以上。

```bash
pip install -r requirements.txt
python -m law_crawl probe                 # 各数据源能否访问
python -m law_crawl update --content-limit 200 --budget-seconds 600
python -m law_crawl manifest              # 数据清单
```

数据默认写在 `data/` 目录（不进版本库），可用环境变量 `LAW_CRAWL_DATA_DIR` 指到别处。

测试：

```bash
pip install pytest ruff
python -m pytest -q
ruff check law_crawl tests
```

## 代码结构

| 文件 | 作用 |
| --- | --- |
| `law_crawl/sync.py` | 命令入口：目录比对、正文批量取回、部门规章同步、可达性探测、数据清单 |
| `law_crawl/flk_client.py` | 国家法律法规数据库的接口客户端 |
| `law_crawl/ministry_rules.py` | 各部门规章库的取数与解析；`SOURCES` 列出已接入的部门 |
| `law_crawl/docx_text.py`、`html_text.py` | 从 docx、详情网页取正文段落，并划分章节与条文 |
| `law_crawl/repository.py` | 两个 SQLite 文件的读写 |
| `law_crawl/service.py` | 标题检索、法规详情等读取函数 |
| `scripts/crawl-loop.sh`、`release-data.sh` | 流水线用的分段采集循环与数据发布 |

接入新的部门：其规章库与 `SOURCES` 里某一类结构相同时加一项即可，结构不同的另写取数函数；
先在本地用 `python -m law_crawl ministries` 核对取到的数量与官网标注是否一致。
