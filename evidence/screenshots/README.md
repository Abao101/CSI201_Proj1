# 报告截图说明

截取日期：2026-11-20。截图为本机实际窗口截图，未合成运行结果。

| 文件 | 可用于报告的内容 |
| --- | --- |
| 01-server-tests.png | Docker 内服务端测试结果，20 项通过 |
| 02-client-tests.png | Docker 内客户端及集成测试结果，46 项通过；包括十客户端双房间用例 |
| 03-docker-images.png | Docker Desktop 的课程镜像 csi201-cw1:latest 和运行中的引擎 |
| 04-login-and-room.png | 登录、五类题目目录、创建房间、第二名玩家加入、未准备时拒绝开始 |
| 05-ready-question-scoring.png | 两人准备、开始比赛、发布题目、正确答案计分、实时排行榜 |
| 06-final-leaderboard.png | 十题结束、获胜者、最终排行榜、结束状态校验与退出 |

01、02 是 VS Code 打开的真实 Docker 测试日志截图，源文件为上级目录 release-zip-tests.txt。04–06 是 VS Code 打开的真实双客户端运行日志截图，源文件为 demo-client-1.txt；不是手工实时终端操作截图。

双客户端演示在课程 Docker 镜像内复用现有 test_two_player_match 集成用例，启动真实服务端及两个真实客户端，通过 TCP 通信。题目来自项目 questions.json（教师题库），未新增题目。Alice 自动答对十题，Bob 自动答错十题，最终为 1000:0。完整双方输出及 SERVER_READY 输出一并保留。

PLAYERS_NOT_READY 是故意在玩家准备前尝试开始；INVALID_STATE 是比赛结束后再次尝试开始。这些是预期校验结果，不是测试失败。退出时 The server closed the connection 是 /quit 后的正常关闭。

本组截图可支持环境、正常流程、计分与测试结果说明；不应将其描述为所有异常场景的逐一手工演示。超时、畸形帧、断线和多房间等细节请结合测试源码和完整测试日志。报告、架构图及团队贡献内容仍需由小组完成。
