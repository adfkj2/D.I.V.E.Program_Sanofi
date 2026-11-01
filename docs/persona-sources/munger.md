# 查理·芒格：voice-v1.0.0 来源与边界

状态：UNVALIDATED。2026-10-31实际读取。英语公开讲话的中文功能性适配；不是本人中文口癖，也没有声学复刻。

## 逐项来源

- [2007 USC演讲转录](https://jamesclear.com/great-speeches/2007-usc-law-school-commencement-address-by-charlie-munger)：讲话日期2007-05-13。实际读正文行17–64、90–117；只声称这些范围。证据m-e1至m-e7分别落在开场、跨学科推理、激励、反证、无解、挫折、脸面段。转载文本未逐句校对录音。
- [1986 Harvard School演讲转录](https://jamesclear.com/great-speeches/how-to-guarantee-a-life-of-misery-by-charlie-munger)：讲话日期1986-06-13。实际读正文行17–69及脚注73；m-e8至m-e11定位自嘲开场、可靠性、挫折、反向推理段。这是洛杉矶Harvard School，不是哈佛大学。页面称转录最初来自BizNews，但本次未打开其原文。
- [USC官方活动页](https://gould.usc.edu/news/usc-law-commencement-2007/)：实际读取行5–24，核对2007-05-13典礼与主讲人；页面未显示具体发布日，不虚填日期。用于活动身份核实，不用于推断讲话习惯。
- [Stripe Press同一USC讲话](https://www.stripe.press/poor-charlies-almanack/talk-ten)：尝试打开，网页无可读正文；不算已读证据。

## 取舍与复核

保留直白判断、自嘲、反讽和承认无解的幅度；没有将“少说话”写成每轮一字回答。lexicon为空：没有用两处独立原文证明某一固定说法是习惯，尤其不把英语转译冒充中文惯用语。

所有7个场景均标AUTHORED，四轮短对话为原创；其evidence_ids仅指向构造语气的依据，不表示历史上发生过这些对话。公开告诫演讲不能充分证明私下安慰、闲聊与来回互动，因此相关trait置信度降低。也不把演讲中的寓言、笑话或转述自动当作可核实史实。

原文仅保留每条2–5词的定位片段；按英文词边界计数，累计USC 19词、1986演讲17词，低于25词。记录不复制整段转录；风格说明为概括，示例为独立创作。未用vendor角色卡作为史料，未运行模型验证。

