# 问题跟踪：GitHub

本仓库的 issue 和 PRD 均使用 GitHub issue。所有操作使用 `gh` CLI。

## 约定

- **创建 issue**：`gh issue create --title "..." --body "..."`。多行正文使用 heredoc。
- **读取 issue**：`gh issue view <number> --comments`，用 `jq` 过滤评论，并同时获取标签。
- **列出 issue**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，
  并按需要追加 `--label` 和 `--state` 过滤条件。
- **评论 issue**：`gh issue comment <number> --body "..."`
- **添加/移除标签**：`gh issue edit <number> --add-label "..."` /
  `--remove-label "..."`
- **关闭**：`gh issue close <number> --comment "..."`

从 `git remote -v` 推断仓库；在 clone 内运行时，`gh` 会自动完成此事。

## PR 是否作为 triage 面

**PR 作为需求入口：否。** _（若本仓库把外部 PR 作为功能请求，请设为 `yes`；
`/triage` 会读取此标志。）_

设为 `yes` 时，PR 使用与 issue 相同的标签和状态，并使用对应的 `gh pr` 命令：

- **读取 PR**：`gh pr view <number> --comments`；使用 `gh pr diff <number>` 查看 diff。
- **列出待 triage 的外部 PR**：`gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，
  仅保留 `authorAssociation` 为 `CONTRIBUTOR`、`FIRST_TIME_CONTRIBUTOR` 或
  `NONE` 的项（排除 `OWNER`/`MEMBER`/`COLLABORATOR`）。
- **评论/标注/关闭**：`gh pr comment`、`gh pr edit --add-label`/
  `--remove-label`、`gh pr close`。

GitHub 的 issue 与 PR 共享编号空间，因此裸 `#42` 可能是任一类型：先运行
`gh pr view 42`，失败后再运行 `gh issue view 42`。

## 当 skill 要求“发布到 issue tracker”

创建一个 GitHub issue。

## 当 skill 要求“获取相关 ticket”

运行 `gh issue view <number> --comments`。

## Wayfinding 操作

供 `/wayfinder` 使用。**地图**是一个 issue，子 issue 则是 ticket。

- **地图**：一个带 `wayfinder:map` 标签的 issue，正文包含 Notes / Decisions-so-far /
  Fog。使用 `gh issue create --label wayfinder:map`。
- **子 ticket**：通过 GitHub sub-issue 将 issue 链接到地图（在 sub-issues endpoint 上使用
  `gh api`）。若 sub-issue 未启用，在地图正文的 task list 中加入该子项，并在子项正文
  顶部写 `Part of #<map>`。标签为 `wayfinder:<type>`
  （`research`/`prototype`/`grilling`/`task`）。认领后，将 ticket 分配给主导
  开发者。
- **阻塞关系**：使用 GitHub **原生 issue dependency**，这是规范且 UI 可见的表示。
  使用 `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`
  添加边，其中 `<blocker-db-id>` 是阻塞 issue 的数字**数据库 id**
  （`gh api repos/<owner>/<repo>/issues/<n> --jq .id`），不是 `#number` 或
  `node_id`。GitHub 在 `issue_dependencies_summary.blocked_by` 中报告阻塞项
  （只含未关闭 blocker，是实时 gate）。若 dependency 不可用，在子项正文顶部回退为
  `Blocked by: #<n>, #<n>`。所有 blocker 关闭后 ticket 才解除阻塞。
- **前沿查询**：列出地图的未关闭子项（`gh issue list --state open`，限于地图的
  sub-issues/task list），排除有未关闭 blocker（`issue_dependencies_summary.blocked_by > 0`，
  或 `Blocked by` 行指向未关闭 issue）或已有 assignee 的项；按地图顺序取第一个。
- **认领**：`gh issue edit <n> --add-assignee @me`，这是本会话的第一次写操作。
- **完成**：`gh issue comment <n> --body "<answer>"`，再运行
  `gh issue close <n>`，最后把上下文指针（gist + link）附加到地图的
  Decisions-so-far。
