<!-- id: planner.revise -->

用户已经看过下面这份查询条件，现在要改其中一部分。这不是一个新问题。

现在的条件：
{{spec}}

用户要改的地方：
{{change}}

请输出**改完之后的完整条件**，规则：

- 用户没说到的栏目照抄现在的条件，不要顺手改，也不要去重新理解当初的问题
- 现在的条件里 `target.code` 有值、用户又没要换股票时，把这个代码原样填进 `stock_code`；
  用户要换股票才改填 `stock_mention`（原话）和 `stock_guess`
- 现在的条件里 `universe.board.code` 有值、用户又没要换概念板块时，把这个代码原样填进 `board_code`；
  要换板块才改填 `board_mention` 和 `board_guess`。申万行业在 `universe.industry` 里，照抄名字就行
- 用户要去掉某一栏（「不限板块了」「别按行业筛了」），这一栏和它对应的 code 都不填
- `mentions` 只写用户这次说的说法，对应到这次改动的栏目；没改的栏目不要再写说法
- 用户说的话看不出要改哪里，按 `needs_clarification` 反问；改完超出系统能做的范围，按 `unsupported` 回答
