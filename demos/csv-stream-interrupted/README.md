# 大表 CSV 导出中断：两种失败的可区分证据

这个目录用两个本地查询复现大表导出中断的两种情形，并给出入库脚本可以依赖的判定依据。

## 运行

```bash
# 在仓库根目录
.venv/bin/python demos/csv-stream-interrupted/make_db.py
.venv/bin/python demos/csv-stream-interrupted/probe.py
```

不需要端口、不需要插件。`probe.py` 通过 Datasette 进程内 ASGI 接口发请求，并把 ASGI 响应还原成 HTTP/1.1 字节，再用标准库 `http.client` 当客户端读取，因此能看到真实的 chunked 终止状态。

## 两条查询

```text
A  /demo/case_a.csv?_stream=1&_size=100&_dl=1&_timelimit=100
B  /demo/case_b.csv?_stream=1&_size=100&_dl=1&_timelimit=100
```

两条都导出同一张 1,000 行的表：

- `case_a`：每一行都足够慢，第一页 SQL 就超时。
- `case_b`：1–500 行很快，501–600 行很慢。第 1–5 页正常写出，第 6 页超时。

## 响应对比

| 观察点 | A：第一页 SQL 超时 | B：CSV 已开始写，后续页超时 |
| --- | --- | --- |
| HTTP 状态 | `500` | `200` |
| `Content-Type` | `text/plain; charset=utf-8` | `text/csv; charset=utf-8` |
| `Content-Disposition` | 无 | `attachment; filename="case_b.csv"` |
| `Content-Length` | 无（chunked） | 无（chunked） |
| 是否收到 chunked 终止块 | 是（错误响应完整结束） | 否（连接被服务端丢弃） |
| 客户端读取结果 | 正常读完 29 字节 | `IncompleteRead`，读到 7,910 字节 |
| 正文开头 | `QueryInterrupted: interrupted` | `id,payload,pause` |
| 完整 CSV 数据行 | 0（正文不是 CSV） | 500（1–500 行） |
| 最后一条完整记录 | 无 | `500,row-0500,0` |
| 是否以 CRLF 结尾 | 否 | **是** |

B 的最后一行以 `\r\n` 结尾，但文件仍然是不完整的。**不能用“最后一行有换行”判断下载完成。**

## 为什么边界正好在第一页

`datasette/views/table.py` 的 `table_view` 先调用 `table_view_data()` 执行第一页 SQL，拿到行之后才构造并返回 `Response.stream()`。HTTP 响应头和第一个 CSV 块都是在第一页 SQL 之后才发出的（`datasette/views/table.py:1956`、`datasette/views/table.py:1978`、`datasette/views/table.py:2014`）。

因此：

- 第一页 SQL 失败：`Response` 还没产生，走普通错误处理，客户端看到非 2xx，正文里没有 CSV 头。
- 第一页成功、第 N 页（N>1）失败：响应头和前面的 CSV 已经发出，`fetch_next_page()` 抛异常（`datasette/views/table.py:1981`），`Response._asgi_send_stream()` 不再发送终止块（`datasette/utils/asgi.py:580`），服务端断开连接（`datasette/app.py:3494`），客户端读到的是没有正常结束的 200 响应。

`?_stream=1` 的第一页大小默认是 `max_returned_rows`（默认 1,000），不是普通表页的 100（`datasette/views/table.py:2483`），所以第一页比普通浏览更容易超时。

另外，同一个 A 类失败换到任意 SQL 查询页会变成 `400`，正文是 “SQL query took too long...”（`datasette/views/database.py:852`）。**因此不能只看状态码判断，必须同时看 Content-Type、CSV 头和传输是否正常结束。**

## 什么条件下可以继续下载

### 1. `?_stream=1` 本身不能续传

`?_stream=1&_next=...` 会被拒绝：

```text
400  _next not allowed when streaming
```

流式响应也没有 `Link: <...>; rel="next"`。所以一个中断的流式 CSV 没有服务端游标可用。

### 2. 非流式分页是可续传的

去掉 `_stream`，用 `_size=N`（N ≤ `max_returned_rows`）一页一页取：

```text
GET /demo/case_b.csv?_size=100&_dl=1
200
Link: <http://localhost/demo/case_b.csv?_size=100&_dl=1&_next=100>; rel="next"
```

`_next` 是排他游标：`_next=500` 从第 501 行开始。它是 keyset 游标，不是字节偏移。续传时必须保持相同的过滤条件和排序参数（`_sort` / `_sort_desc`），并原样复用 `Link` 里的 `_next`，不要自己拼。

分页模式还有一个关键性质：**一页要么完整返回，要么在发送任何字节之前失败**。所以失败页可以整页重试，不会留下半个 CSV。

### 3. 用过滤条件续传

当导出顺序就是某个唯一键的顺序时，可以用最后一条完整记录的键继续：

```text
GET /demo/case_b.csv?_size=100&_dl=1&id__gt=600
200  601..700
```

这个写法只在 `id` 唯一、且导出按 `id` 升序时精确。降序要用 `id__lt`；复合主键、可空列、文本排序需要把游标条件写完整。不要把它当成通用方案。

### 4. `_timelimit` 只能调小，不能调大

`?_timelimit=` 只会在小于服务端 `sql_time_limit_ms` 时生效（`datasette/database.py:732`）。用 `_timelimit=5000` 去救一个 100ms 限制下的失败页仍然会超时。要么把 `_size` 调小，要么改服务端设置并重启。

## 给入库脚本的判定依据

按下面的顺序判定，不要先看状态码：

```text
1. 记录响应元数据：status、content-type、content-disposition、是否正常收到完整响应体。

2. 不是 200，或 content-type 不是 text/csv（TSV 同理），
   或正文第一条记录不等于预期表头：
       -> 导出从未成功产生 CSV（A 类）。
       -> 不存在可续传的半文件；从头发起，或从上次保存的游标重新开始。

3. 200 + text/csv + 正确的 Content-Disposition + 第一行是预期表头，
   但客户端读到 IncompleteRead / ChunkedEncodingError /
   "transfer closed with outstanding read data remaining"：
       -> CSV 已开始写，之后中断（B 类）。
       -> 丢弃最后一条不完整记录，保留最后一个完整记录的排序键。

4. 200 + text/csv + 响应正常结束：
       -> 本次响应完整。
       -> 仍建议用已知总行数或独立 count 校验（有并发写入时除外）。
```

配套的续传规则：

```text
- 流式请求（_stream=1）中断后，不能用 _next 续传（会被 400 拒绝）。
  改为分页模式：?_size=N&_next=<服务端返回的游标>，
  或在该导出顺序确实按唯一键时用 last_key 过滤。

- 最稳妥的入库方式：一开始就不用 _stream。
  逐页请求 ?_size=N&_dl=1，跟随 Link rel="next"，
  每页成功后再落盘；某页失败时原样重试该页 URL。

- 续传前校验：新文件第一条数据的键必须正好排在 last_key 之后；
  记录游标、过滤条件、排序参数和页大小，四者缺一不可。
```

## 常见误判

- **看最后一行有没有换行**：B 的最后一行有 `\r\n`，文件仍然缺 500 行。
- **看 Content-Length**：CSV 导出是 chunked，两个响应都没有 `Content-Length`。
- **看状态码**：A 在表页是 `500`，在 `/-/query.csv` 是 `400`，都不能单独作为分类依据。
- **把 `_next` 当偏移量**：它是排他 keyset 游标，且和排序/过滤绑定。
- **用 `_timelimit` 调大限额**：它只能调小。
