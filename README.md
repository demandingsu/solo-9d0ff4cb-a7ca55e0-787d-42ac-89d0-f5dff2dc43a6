# 公交刷卡历史数据清退台账

客流周报要能按原口径复算，同时腾出历史刷卡数据占用的空间。本工具导入在库文件清单与周报日记录，
勾选必须下线的文件后，为所有周报日各选一种复算依据，求出**留存文件并集总字节最少**的方案
（共用文件只计一次），列出逐日复算依据与清退清单。

## 规则

一个周报日 = 周报编号 + 日期 + 车队范围 + 归站字典版本，有两类复算依据，二选一：

1. **日汇总**：日期、车队范围、字典版本三项与周报日完全吻合；
2. **刷卡分区 + 归站字典**：该日期、该车队范围内每个车队的全部刷卡分区，加上该版本的一份归站字典。

- 勾选“必须下线”的文件不得出现在任何复算依据里，并且一定进入清退清单。
- 任一周报日两类依据都凑不齐时，整体**暂停清退**，页面逐条列出缺失的周报编号与日期。
- 确认只更新“在库登记”，不删除磁盘文件；下次核算按剩余文件进行。
- 周报记录、文件清单或下线勾选发生变化后，旧清退单会标记过期，必须重新核算才能确认。

## 输入格式

在库文件清单 CSV（首行表头，列名支持中文，大小支持 `1.5GiB` / `1,234` 这类写法）：

```csv
file_id,type,size_bytes,date,fleet,fleet_scope,dict_version
AGG-20260516-12-v3,daily_agg,12000000,2026-05-16,,1;2,v3
TAP-20260516-F1,tap,4000000,2026-05-16,1,,
DICT-v3,dict,8000000,,,,v3
LOG-2025-Q4.txt,other,500000,,,,
```

- `type`：`daily_agg`（日汇总）/ `tap`（刷卡分区）/ `dict`（归站字典）/ `other`
- `daily_agg` 需要 date、fleet_scope、dict_version；`tap` 需要 date、fleet；`dict` 需要 dict_version
- `fleet_scope` 写 `1;2` 表示车队 1 和 2，写 `*` 或 `全部车队` 表示全部车队；与周报日按**严格相等**匹配

周报日记录 CSV（最多 12 条）：

```csv
report_no,date,fleet_scope,dict_version
W2026-21,2026-05-16,1;2,v3
W2026-21,2026-05-18,*,v3
```

页面“载入示例”可直接载入 `static/samples/` 下的两份示例。

## 运行

Docker Compose：

```bash
docker compose up --build -d
# 打开 http://localhost:8000
```

状态持久化在命名卷 `clearance-data`（容器内 `/app/data/state.json`）。

本地开发（Python 3.11+）：

```bash
pip install -r requirements.txt
python app.py            # http://localhost:8000
```

环境变量：`DATA_DIR`（状态目录，默认 `./data`）、`SECRET_KEY`、`PORT`（仅 `python app.py`）。

## 测试

```bash
pip install pytest
python -m pytest tests -q
```

覆盖：共用分区只计一次、依据回退（日汇总被勾选下线时改用分区+字典）、缺刷卡分区/缺字典/缺日汇总时
按周报与日期阻断、求解结果与暴力穷举一致、12 条上限、旧清退单在周报/清单/勾选变化后被拒、确认只改登记、
CSV 导出。

## 代码结构

- `importer.py`：CSV 解析、字段校验、状态指纹
- `planner.py`：复算依据枚举、最小留存求解、缺失诊断
- `app.py`：Flask 路由与状态持久化
- `templates/`、`static/`：页面与样式
