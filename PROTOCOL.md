# Networked Quiz Tournament 应用层协议 v1

状态：供客户端与服务端共同实现的初版约定。修改消息格式或行为前，双方须同步更新本文档。

本文以 Coursework I Task Sheet 为强制依据，以 Application Layer Protocol Reference 为参考。下文的容量、开局、排名、离线和消息细节是本组选定的设计，不是教师额外要求。实现仅使用 Python 3.12 标准库和原始 TCP socket，界面仅限命令行。

## 1. 共同约定与职责

- 每个客户端保持一条 TCP 连接。连接先登录，再执行操作；身份绑定连接，不能用另一条连接恢复会话。
- 服务端独占读取和校验题库，决定房间状态、题序、截止时间、答案是否接受及分数。客户端只负责输入、展示和发送请求。
- 用户一次最多加入一个房间。房间最多 10 人；服务端整体须支持至少 10 条已登录连接和至少两个同时比赛的房间。
- 默认不随机出题，不提供密码、聊天、旁观、断线续局或持久化战绩。重启后会话和房间消失。
- 题库全量校验成功并开始监听后，服务端输出 `SERVER_READY`。

启动命令必须支持：

```text
python3 server.py --host <server_IP> --port <port> --questions <json_path>
python3 client.py --server <server_IP> --port <port> --id <user_id> --name <display_name>
```

## 2. TCP 分帧与 JSON 编码

每帧为 `4 字节无符号大端整数 N + N 字节 UTF-8 JSON`。N 是编码后的字节数，不是字符数，范围为 1～65536（含边界）。Python 可使用 `struct.pack('!I', len(body))`。

- JSON 顶层必须为对象；禁止重复对象键、NaN、Infinity 和 UTF-8 BOM。整数类型不接受布尔值；未知对象字段忽略以便兼容，已规定字段必须校验。
- 每条连接独立缓存接收字节：不足 4 字节继续读；拿到长度后等待完整负载；取出一帧后继续解析缓存中的下一帧。不能假设一次 `recv` 等于一条消息。
- 发送端必须处理部分写入。同一连接的帧必须串行发送，不能交错。慢客户端不能阻塞其他连接或房间。
- 长度非法、UTF-8 非法、JSON 无法解析或顶层非对象时关闭该连接，不要求发送错误帧；服务端清理其身份和房间关系。
- JSON 对象能够解析但信封或业务字段非法时，返回错误响应并保留连接。
- TCP 返回空字节表示断开；未完成帧丢弃并执行离线处理。
- 登录须在连接建立后 30 秒内完成；一帧从收到首字节到收齐最多 30 秒。已登录且没有半帧的空闲连接不因无操作而超时。
- 每连接待发送数据上限为 1 MiB；超过上限或队列非空且连续 30 秒无发送进展时，断开该慢连接。

## 3. 消息信封

所有消息均包含以下六个字段，不适用的字段显式写为 `null`：

```json
{"version":1,"type":"LOGIN_REQUEST","request_id":"req-1","event_id":null,"session_id":null,"payload":{"user_id":"student1","name":"Alice"}}
```

| 字段 | 类型与规则 |
| --- | --- |
| version | 整数，固定为 1 |
| type | 本文定义的大小写敏感消息名 |
| request_id | 请求和响应为 1～64 个 ASCII 字母、数字、`_`、`-`；事件为 null |
| event_id | 请求和响应为 null；事件为服务端生成的非空字符串 |
| session_id | 登录请求为 null；登录成功后为服务端返回的会话字符串 |
| payload | 对象，即使无参数也发送 `{}` |

客户端在一条连接中按 `req-1`、`req-2` 等生成不重复的请求 ID。服务端每连接保留最近 1024 个格式有效的请求 ID；命中时返回 `DUPLICATE_REQUEST`，不再次执行。更早的 ID 不保证去重，客户端不得重用 ID，不自动重发未确认的修改请求。

每个完整、合法请求恰好得到一个直接响应：成功响应或 `ERROR_RESPONSE`。成功响应类型将 `_REQUEST` 替换为 `_RESPONSE`。响应复用请求 ID；无法从非法信封提取合法 ID 时错误响应的 request_id 为 null。

登录成功响应的 session_id（信封及 payload 中）为新会话 ID。其余服务端消息使用接收连接当前会话 ID；未登录时为 null。服务端不回显不可信的请求 session_id。请求中的 session_id 必须与当前连接绑定值相同，否则拒绝。

事件 ID 在一次服务端运行中唯一。事件通过同一 TCP 连接送达，并可穿插于其他请求响应之间。客户端必须持续接收，不能仅在用户输入后调用接收。

## 4. 数据对象

| 对象 | 完整字段 |
| --- | --- |
| QuizSummary | `quiz_id: string, title: string, description: string, question_count: integer` |
| Player | `user_id: string, name: string, ready: boolean, is_host: boolean` |
| RoomSummary | `room_code: string, quiz_id: string, title: string, host_user_id: string, player_count: integer, capacity: integer, state: string` |
| RoomView | RoomSummary 的全部字段，加 `players: Player[]` |
| RankingEntry | `rank: integer, user_id: string, name: string, score: integer, active: boolean` |

`user_id` 为 1～32 个 ASCII 字母、数字、`_`、`-`，大小写敏感，在线 ID 唯一。`name` 为 1～32 个 Unicode 字符，须包含非空白字符且不得包含控制字符；保留原值，允许重名，以 user_id 区分。房间码是服务端生成的 6 位大写字母或数字，同一运行期间不复用。客户端可将用户输入的房间码转换为大写，线上值必须符合格式。

RoomView 中 players 按加入顺序排列，只包含仍在房间中的用户。排行榜包含开局时的全部玩家，包括中途退出者；active 表示是否仍在该房间。分数降序排列，同分按 ASCII user_id 升序排列；rank 为排序后从 1 开始的连续序号，不并列。

## 5. 请求与成功响应

以下表格列出 payload 的全部必需字段。`无` 表示 `{}`。所有响应中的 `success` 固定为 true；失败统一使用第 8 节的错误响应。

| 请求类型 | 请求 payload | 成功响应 payload | 条件 |
| --- | --- | --- | --- |
| LOGIN_REQUEST | `user_id, name` | `success, session_id` | 尚未登录；重复在线 ID 拒绝 |
| LIST_QUIZZES_REQUEST | 无 | `quizzes: QuizSummary[]` | 已登录；按题库顺序 |
| LIST_ROOMS_REQUEST | 无 | `rooms: RoomSummary[]` | 已登录；仅 LOBBY 且未满，按创建顺序 |
| CREATE_ROOM_REQUEST | `quiz_id: string` | `success, room: RoomView` | 未在房间；创建者自动加入并成为房主，ready=false |
| JOIN_ROOM_REQUEST | `room_code: string` | `success, room: RoomView` | 未在房间；目标 LOBBY 且未满；ready=false |
| PLAYER_LIST_REQUEST | 无 | `players: Player[]` | 在任意状态的房间中 |
| READY_REQUEST | `ready: boolean` | `success, ready: boolean` | LOBBY；重复设置相同值成功但不发变更事件 |
| START_QUIZ_REQUEST | 无 | `success, room_code: string` | 房主；LOBBY；至少 2 人且所有人（含房主）准备 |
| ANSWER_REQUEST | `question_id: string, answer: string` | `accepted: true, question_id: string` | 当前题开放、未到截止、尚未回答；answer 仅 A/B/C/D |
| LEADERBOARD_REQUEST | 无 | `room_code: string, leaderboard: RankingEntry[]` | 在房间；LOBBY 返回当前成员的零分排名 |
| LEAVE_ROOM_REQUEST | 无 | `success, room_code: string` | 在房间；退出后恢复已登录无房间状态 |
| LOGOUT_REQUEST | 无 | `success` | 已登录；自动退出房间，响应排队发送后关闭连接 |

客户端请求中不发送 user_id 来指定被操作玩家；服务端始终使用连接绑定的身份。除 ANSWER_REQUEST 的 accepted 外，表中只写字段名的 `success` 均为布尔值，session_id 为字符串。

改变状态的成功响应应先排入请求者的发送队列，再排入由本次操作产生的事件。服务端必须按连接保持该顺序。客户端不得把“响应之后的下一帧”假定为某个固定事件。

## 6. 服务端事件

所有事件 payload 必须含 `room_code: string`，只发送到相关房间成员，不向其他房间广播。同一逻辑事件广播给不同玩家时可共享 event_id；信封 session_id 按接收者填写。

| 事件类型 | 除 room_code 外的必需字段 | 时机 |
| --- | --- | --- |
| PLAYER_STATUS_EVENT | `players: Player[], host_user_id: string` | 创建、加入、准备变化、离开或掉线后的最新成员状态 |
| QUIZ_STARTED_EVENT | `quiz_id: string, title: string, total_questions: integer` | 开局成功后，第一题之前 |
| QUESTION_EVENT | `question_id: string, number: integer, total_questions: integer, text: string, options: object, time_limit: integer, points: integer` | 每题开始；number 从 1 开始；options 恰为 A/B/C/D 的字符串映射 |
| QUESTION_RESULT_EVENT | `question_id: string, correct_answer: string, your_answer: string或null, points_awarded: integer, leaderboard: RankingEntry[]` | 本题关闭后；每位接收者得到自己的答案和得分 |
| QUIZ_FINISHED_EVENT | `quiz_id: string, final_leaderboard: RankingEntry[], winner: RankingEntry` | 最后一题结果之后；winner 为排行榜首项 |

不发送附录示例中的 ROOM_CLOSED_EVENT：本设计房主离开时转移房主；空房间直接删除，无接收者。客户端只处理本文定义的事件。

## 7. 房间生命周期、时间与计分

状态流程：`LOBBY → QUESTION_OPEN → QUESTION_CLOSED → 下一题 QUESTION_OPEN 或 FINISHED`。

- LOBBY 中房主退出或掉线，最早加入的剩余成员成为房主，其他人的准备状态保留。所有人退出则删除房间。
- START 的条件检查与状态转换必须原子完成；开局后禁止加入，不能再改变 ready 或重新开始同一房间。
- 开局时固定参赛名单并将分数归零。题目按文件顺序依次发布；服务端在将 QUESTION_EVENT 排入各玩家发送队列前，以单调时钟记录起点，截止为起点加题目 time_limit。
- ANSWER_REQUEST 完整解码并进入服务端业务处理时读取单调时钟，只有 `now < deadline` 才可接受。客户端时间戳和屏幕倒计时不作为判定依据。
- 同时满足房间、题号、选项、期限和未作答条件后才记录答案。错误请求不占用答题机会；已接受答案不可修改。
- 答案接受响应只说明记录成功，不透露正误，也不提前更新公开排行榜。
- 全部当前活跃玩家已回答或期限到达时关闭题目；截止检查优先于接受新答案。即使定时器回调延迟，也不得接受迟到答案。
- 关闭时，已接受且正确的答案获得题库 points；错误或缺答得 0 分。重复或迟到请求不新增分数，也不撤销之前已接受答案的得分。
- 每题只结算一次。先发送 QUESTION_RESULT_EVENT，等待 2 秒再开启下一题；最后一题结果之后立即发送 QUIZ_FINISHED_EVENT。
- 客户端倒计时从收到题目后作近似展示，网络延迟可能导致服务端先截止；以服务端响应和结果为准。
- 比赛中离开或掉线：从活跃成员移除，保留参赛记录、已接受答案和累计分；房主按加入顺序转移，比赛自动继续，不需房主操作。重新登录不能重返正在运行的房间。
- 若移除玩家后剩余活跃玩家均已作答，立即关闭该题；若没有活跃成员，删除房间并取消其定时任务。
- FINISHED 保留房间和成绩供当前成员查看，退出到最后一人时删除；重玩须先退出再创建新房间。
- 服务端 Ctrl+C 停止监听并关闭所有连接、取消定时任务、释放资源；不定义关机事件。客户端遇到 EOF 显示连接已关闭并退出。客户端 Ctrl+C 可直接关闭 socket，由服务端按掉线处理。

并发实现可以使用 selectors 或线程，但房间状态必须有明确所有者或锁。检查开局、记录答案、关闭结算、转移房主均为完整原子操作。不得持全局锁执行阻塞网络发送。

## 8. 错误响应与校验顺序

```json
{"version":1,"type":"ERROR_RESPONSE","request_id":"req-8","event_id":null,"session_id":"s-example","payload":{"code":"DUPLICATE_ANSWER","message":"An answer has already been accepted for this question."}}
```

message 为供人阅读的字符串，客户端逻辑仅依赖 code，不匹配英文句子。

| code | 含义 |
| --- | --- |
| INVALID_MESSAGE | 信封或 payload 缺字段、类型/长度/格式错误 |
| UNSUPPORTED_VERSION | version 是整数但不为 1 |
| UNKNOWN_COMMAND | type 为字符串但不是支持的请求类型 |
| DUPLICATE_REQUEST | 最近请求 ID 重复 |
| NOT_AUTHENTICATED | 尚未登录却发送业务请求 |
| ALREADY_AUTHENTICATED | 已登录再次 LOGIN |
| INVALID_SESSION | 请求会话 ID 与连接不符 |
| DUPLICATE_USER | 用户 ID 已在线 |
| QUIZ_NOT_FOUND | 题库不存在 |
| ROOM_NOT_FOUND | 房间不存在 |
| ROOM_FULL | 房间已满 |
| ALREADY_IN_ROOM | 已在房间却创建/加入 |
| NOT_IN_ROOM | 操作要求房间，但用户不在房间 |
| INVALID_STATE | 房间状态不允许该操作，包括加入已开局房间 |
| NOT_ROOM_HOST | 非房主请求开局 |
| NOT_ENOUGH_PLAYERS | 少于 2 人 |
| PLAYERS_NOT_READY | 有成员未准备 |
| QUESTION_CLOSED | 没有开放题、已到截止或提交已结束的题 |
| INVALID_ANSWER | answer 非 A/B/C/D，或题号不是当前题且不是本场已结束题 |
| DUPLICATE_ANSWER | 当前题中该玩家已成功回答 |
| SERVER_ERROR | 未预期内部错误；记录不泄露答案的服务端诊断 |

一般按以下顺序检查并只报告首个错误：信封结构 → 版本 → 请求类型 → 重复 ID → 登录/会话 → payload 字段 → 房间成员关系 → 业务状态/权限 → 业务条件。登录请求只要求 null 会话，不执行已登录会话匹配；已登录的再次登录返回 ALREADY_AUTHENTICATED。

START 的业务顺序为状态、房主、人数、准备。JOIN 的业务顺序为自身不在房间、目标存在、LOBBY、容量。ANSWER 在字段类型合法后依次检查：处于房间、是否存在开放题及期限、本场已结束题号、当前题号和选项、重复答案。LOBBY/QUESTION_CLOSED/FINISHED 中提交答案统一为 QUESTION_CLOSED；下一题开放时提交上一题同样返回 QUESTION_CLOSED。

## 9. 客户端命令映射与典型顺序

| CLI 命令 | 行为 |
| --- | --- |
| `/help` | 本地显示帮助 |
| `/quizzes`、`/rooms` | LIST_QUIZZES、LIST_ROOMS |
| `/create <quiz_id>`、`/join <room_code>` | CREATE_ROOM、JOIN_ROOM |
| `/players` | PLAYER_LIST |
| `/ready`、`/unready` | READY，ready 为 true/false |
| `/start` | START_QUIZ |
| `/answer <A\|B\|C\|D>` | ANSWER，使用最近开放题的 question_id，可在本地把小写选项转大写 |
| `/score`、`/leave`、`/quit` | LEADERBOARD、LEAVE_ROOM、LOGOUT |

上表省略消息名的 `_REQUEST` 后缀。客户端启动连接成功后自动 LOGIN。客户端应维护当前房间、当前题号和等待响应的 request_id；离开成功后清空房间和题目状态。收到未知事件时报告协议不兼容并关闭连接，不静默猜测其含义。

正常双人流程：登录 → 查询题库 → A 创建房间 → B 加入 → 双方 ready → A start → START 响应 → QUIZ_STARTED_EVENT → QUESTION_EVENT → 双方 ANSWER 响应 → QUESTION_RESULT_EVENT → 下一题 → 最终结果 → QUIZ_FINISHED_EVENT → 查询分数或退出。

首题事件示例（实际时间和分值来自题库）：

```json
{"version":1,"type":"QUESTION_EVENT","request_id":null,"event_id":"evt-10","session_id":"s-example","payload":{"room_code":"AB12CD","question_id":"NET001","number":1,"total_questions":5,"text":"Which transport protocol provides reliable byte-stream delivery?","options":{"A":"UDP","B":"TCP","C":"ARP","D":"ICMP"},"time_limit":15,"points":100}}
```

客户端回答示例：

```json
{"version":1,"type":"ANSWER_REQUEST","request_id":"req-9","event_id":null,"session_id":"s-example","payload":{"question_id":"NET001","answer":"B"}}
```

## 10. 联调验收清单

所有正式执行证据须在教师提供的 Docker 环境中取得；此清单不是已通过测试的声明。

- 单帧拆成逐字节发送、多个帧一次发送、中文 JSON，均正确解码且不丢帧。
- 零长/超长、非法 UTF-8/JSON、重复键、非对象关闭单个连接；字段错误、错误版本、错误会话和未知请求产生指定错误。
- 登录、重复在线 ID、请求 ID 重复、退出后再次登录符合约定。
- 双人完成创建、加入、准备、开局、全部题目、排名和退出；响应与事件可正确区分。
- 非房主、未准备、单人开局、满房、开局后加入均拒绝；重复 ready 不产生多余状态事件。
- 正确、错误、缺答、重复、迟到、旧题号及非法选项覆盖；期限边界以服务端单调时间为准；重复提交不撤销首次得分。
- 目录不包含题目文本/选项/答案；开放题和接受响应不含答案或正误；关闭时才更新成绩。
- 至少 10 个客户端、两个房间同时比赛，题目、事件、计时、分数无跨房间影响。
- 等候和比赛中房主/普通玩家掉线，房主转移、答案保留、全员离开删除与定时器清理正确。
- 慢读客户端、半帧超时及畸形消息不阻塞其他客户端；服务端和客户端 Ctrl+C 后正常释放资源。
- 分数相同的排名稳定；FINISHED 可查分、不可再开局；重新创建房间获得全新比分。

## 11. 实现分工

- 服务端：题库校验、连接与身份、请求校验、房间状态机、计时计分、广播、异常清理及对应测试。
- 客户端：参数与命令行交互、持续接收、请求 ID、响应匹配、事件显示、本地状态、断开提示及对应测试。
- 双方共享：本协议和分帧编解码模块；变更字段时先更新文档并沟通，再同时调整两端。开发文档须记录实际实现与本文的差异，不得只引用教师附录代替自己的协议设计。
