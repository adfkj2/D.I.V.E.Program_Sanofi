import assert from "node:assert/strict";
import { mkdir, writeFile, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const here=path.dirname(fileURLToPath(import.meta.url));
const artifacts=path.resolve(here,"../artifacts/upgrade-e2e");
const base=process.env.WOT_E2E_URL||"http://127.0.0.1:8765";
if(process.env.WOT_E2E_ALLOW_WRITE!=="1")throw new Error("Use WOT_E2E_ALLOW_WRITE=1 with an isolated e2e database.");
if(!["127.0.0.1","localhost","[::1]"].includes(new URL(base).hostname))throw new Error("E2E only supports a local isolated test server.");
assert.equal((await (await fetch(`${base}/api/v1/healthz`)).json()).test_server,true,"Refuse writes: start tests/e2e_server.py with a new isolated e2e directory.");
const initial=await (await fetch(`${base}/api/v1/state`)).json();
for(const key of ["conversations","evidence","directives","versions","simulations","growth"])assert.equal(initial[key].length,0,`E2E requires a fresh database: ${key}`);
for(const key of ["connections","skill_imports","virtual_sessions"])assert.equal(initial.upgrade[key].length,0,`E2E requires a fresh database: upgrade.${key}`);
assert.equal(initial.upgrade.me.bio,"");
await mkdir(artifacts,{recursive:true});
const browser=await chromium.launch({headless:true,channel:process.env.WOT_E2E_CHANNEL||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1050},reducedMotion:"reduce"});
const page=await context.newPage();
// Spare-port isolated servers retain the same allowed origin as production.
await page.route("**/assets/**",async route=>{const response=await route.fetch({headers:{...route.request().headers(),origin:"http://127.0.0.1:8765"}});await route.fulfill({response});});
await page.route("**/api/v1/**",async route=>{const response=await route.fetch({headers:{...route.request().headers(),origin:"http://127.0.0.1:8765"}});await route.fulfill({response});});
const errors=[],external=[],checks=[],screenshots=[],messagePosts=[];
page.on("pageerror",error=>errors.push(error.message));
page.on("request",request=>{if(!request.url().startsWith(base)&&!request.url().startsWith("data:"))external.push(request.url());});
page.on("request",request=>{if(request.method()==="POST"&&/real-conversations\/[^/]+\/(?:messages|turns)$/.test(new URL(request.url()).pathname))messagePosts.push({body:request.postDataJSON(),key:request.headers()["idempotency-key"]});});
let fixture="none",fixtureTask=null,fixtureSession=null;
let validationOutcome="TEST_FAILED",validationDelay=150,validationCalls=0;
const fixtureConnection={id:"fixture-only-no-backend-key",provider:"openai",model:"visual-fixture-model",key_status:"CONFIGURED",api_key_mask:"••••••••",storage:"SESSION_ONLY",is_default:true};
let realState;
await page.route("**/api/v1/model-discovery",async route=>route.fulfill({json:["fixture-model-a","fixture-model-b"]}));
await page.route("**/api/v1/model-connection-validations",async route=>{
  validationCalls++;const draft=route.request().postDataJSON();const outcome=validationOutcome;await new Promise(resolve=>setTimeout(resolve,validationDelay));
  const saved=["ADDED","UPDATED"].includes(outcome);
  if(saved)fixture="configured";
  await route.fulfill({json:{status:outcome,tested:outcome!=="TEST_FAILED",saved,provider:draft.provider,model:draft.model,elapsed_ms:286,message:outcome==="TEST_FAILED"?"测试夹具：鉴权失败，请检查 API Key。":outcome==="SAVE_FAILED"?"测试夹具：系统安全凭据库暂不可用。":"连接已通过测试并保存。",...(saved?{connection:{...fixtureConnection,model:draft.model}}:{})}});
});
await page.route("**/api/v1/state",async route=>{
  const response=await route.fetch();const state=await response.json();realState=state;
  if(fixture!=="none")state.upgrade.connections=[{...fixtureConnection,...(fixture==="missing"?{key_status:"MISSING",api_key_mask:""}:{})}];
  if(fixture==="virtual"){
    state.upgrade.me.self_admission={eligible:true,version_id:"fixture-certified-v1",assessment_id:"fixture-self-quality",protocol:"character-quality-v1"};
    state.upgrade.me.certified={id:"fixture-certified-v1",text:"视觉夹具，不是正式校验。",status:"VALID"};
    state.upgrade.virtual_sessions=fixtureSession?[fixtureSession]:[];
  }
  await route.fulfill({response,json:state});
});
await page.route("**/api/v1/assessments/fixture-*",async route=>route.fulfill({json:fixtureTask}));
await page.route("**/api/v1/virtual-participants",async route=>{
  if(fixture!=="virtual")return route.continue();
  await route.fulfill({json:[
    {id:"fixture-self",version_id:"fixture-certified-v1",kind:"SELF",display_name:"测试用户",avatar_url:realState?.upgrade.me.avatar_url},
    {id:"fixture-munger",version_id:"munger-v1",kind:"CHARACTER",display_name:"查理·芒格",avatar_url:"/portraits/munger.png"},
    {id:"fixture-buffett",version_id:"buffett-v1",kind:"CHARACTER",display_name:"巴菲特",avatar_url:"/portraits/buffett.png"}
  ]});
});
await page.route("**/api/v1/virtual-sessions/fixture-session",async route=>route.fulfill({json:fixtureSession}));
await page.route("**/api/v1/virtual-sessions/fixture-session/takeover",async route=>{
  fixtureSession={...fixtureSession,state:"INTERVENING"};await route.fulfill({json:fixtureSession});
});
async function nav(label){await page.locator(".sidebar nav").getByRole("button",{name:label,exact:true}).click();await page.waitForTimeout(120);}
async function hash(value){await page.goto(`${base}/#${value}`);await page.locator(".connection-state").waitFor({state:"hidden"});await page.waitForTimeout(140);}
async function state(){return (await context.request.get(`${base}/api/v1/state`)).json();}
async function waitVirtual(check){for(let attempt=0;attempt<150;attempt++){const value=(await state()).upgrade.virtual_sessions[0];if(value&&check(value))return value;await page.waitForTimeout(100);}throw new Error("Virtual session did not reach its expected stable state");}
async function capture(name){const file=`${name}.png`;await page.screenshot({path:path.join(artifacts,file),fullPage:true});screenshots.push(file);}
async function noOverflow(){const size=await page.evaluate(()=>({viewport:innerWidth,width:document.documentElement.scrollWidth,body:document.body.scrollWidth}));assert.ok(size.width<=size.viewport+1&&size.body<=size.viewport+1,JSON.stringify(size));}
async function confirm(button){await page.locator(".confirm-dialog").getByRole("button",{name:button,exact:true}).click();await page.locator(".confirm-dialog").waitFor({state:"hidden"});}
async function choose(slug){await page.locator(".people-column").waitFor({state:"attached"});const search=page.getByLabel("搜索人物或对话");if(!await search.isVisible())await page.getByRole("button",{name:"返回人物列表",exact:true}).click();await search.fill("");await page.getByRole("tab",{name:/^人物/}).click();await page.locator(`[data-testid="contact-row"][data-slug="${slug}"]`).click();await page.locator(".loading-messages").waitFor({state:"hidden"});}
async function send(text){await page.getByLabel("写下你想聊的话",{exact:true}).fill(text);await page.getByRole("button",{name:"发送消息",exact:true}).click();}
async function reply(){await page.locator(".reply-pending").first().waitFor({state:"hidden"});await page.locator(".chat-typing").waitFor({state:"hidden"});assert.equal(await page.locator(".reply-failed").count(),0);}
function record(value){checks.push(value);console.log(value);}
try{
  const sessionToken=(await (await context.request.get(`${base}/api/v1/session`)).json()).token;
  await context.request.post(`${base}/api/v1/preferences`,{headers:{"X-WOT-Token":sessionToken},data:{setup_dismissed:false}});
  await page.goto(base);
  const modelDialog=page.getByRole("dialog",{name:"模型配置",exact:true});await modelDialog.waitFor();
  assert.equal(await modelDialog.getByLabel("模型供应商").locator("option").count(),9);
  await modelDialog.getByLabel("模型供应商").selectOption("qwen");await modelDialog.getByLabel("地区",{exact:true}).waitFor();
  await modelDialog.getByLabel("地区",{exact:true}).selectOption("workspace");await modelDialog.getByLabel("工作空间 ID（按需）").waitFor();
  await modelDialog.getByLabel("模型供应商").selectOption("doubao");await modelDialog.getByLabel("模型或推理接入点 ID").waitFor();
  await modelDialog.getByLabel("模型供应商").selectOption("openai");await modelDialog.getByLabel("API Key",{exact:true}).fill("fixture-only-key-never-saved");await modelDialog.getByLabel("API Key",{exact:true}).blur();await modelDialog.getByText("已读取 2 个模型，也可以手动填写。",{exact:true}).waitFor();assert.equal(await modelDialog.locator("#available-models option").count(),2);await modelDialog.getByLabel("API Key",{exact:true}).fill("");
  await capture("first-run-model-1440");
  await page.setViewportSize({width:390,height:844});await noOverflow();await capture("first-run-model-390");
  await modelDialog.getByRole("button",{name:"稍后设置",exact:true}).click();await modelDialog.waitFor({state:"hidden"});
  assert.equal(await page.locator(".sidebar nav").getByRole("button").count(),4);
  assert.equal(await page.getByTestId("contact-row").filter({has:page.locator('[data-builtin="true"]')}).count(),0); // Built-in marker belongs to the row itself.
  assert.equal(await page.locator('[data-testid="contact-row"][data-builtin="true"]').count(),15);
  record("首次配置支持9种连接；通义地区/工作空间、豆包接入点字段；稍后设置可浏览15位人物，虚拟空间入口始终可见。");

  await page.setViewportSize({width:1440,height:1050});await nav("设置");
  const modelForm=page.locator(".up-connection-form");const keyInput=modelForm.getByLabel("API Key",{exact:true});
  await modelForm.getByLabel("模型 ID",{exact:true}).fill("fixture-test-model");await keyInput.fill("fixture-only-key-never-saved");
  const beforeValidation=validationCalls;await modelForm.evaluate(form=>{form.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));form.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));});
  assert.equal(await keyInput.inputValue(),"fixture-only-key-never-saved");await modelForm.getByRole("heading",{name:"测试失败",exact:true}).waitFor();assert.equal(validationCalls,beforeValidation+1);assert.equal(await keyInput.inputValue(),"fixture-only-key-never-saved");assert.equal((await state()).upgrade.connections.length,0);
  validationDelay=400;await modelForm.getByRole("button",{name:"测试并添加",exact:true}).click();await modelForm.getByLabel("模型 ID",{exact:true}).fill("fixture-edited-model");await modelForm.getByText("表单已修改，上一次测试结果不再适用于当前内容。请重新测试并保存。",{exact:true}).waitFor();assert.equal(await modelForm.getByRole("heading",{name:"测试失败",exact:true}).count(),0);
  validationDelay=150;validationOutcome="SAVE_FAILED";await modelForm.getByRole("button",{name:"测试并添加",exact:true}).click();await modelForm.getByRole("heading",{name:"连接成功，但保存失败",exact:true}).waitFor();assert.equal(await keyInput.inputValue(),"fixture-only-key-never-saved");assert.equal((await state()).upgrade.connections.length,0);await capture("model-save-failure-1440");
  validationOutcome="ADDED";await modelForm.getByRole("button",{name:"测试并添加",exact:true}).click();await modelForm.getByRole("heading",{name:"已添加",exact:true}).waitFor();assert.equal(await modelForm.getByLabel("API Key",{exact:true}).count(),0);assert.ok(await modelForm.getByLabel("API Key 状态").getByText("仅本次运行有效",{exact:true}).isVisible());assert.ok(await modelForm.getByRole("button",{name:"开始聊天",exact:true}).isVisible());await page.locator(".up-connections").waitFor();await page.waitForTimeout(80);const successBounds=await modelForm.getByRole("button",{name:"开始聊天",exact:true}).boundingBox();assert.ok(successBounds&&successBounds.y>=0&&successBounds.y+successBounds.height<=1050);await noOverflow();await capture("model-added-1440");
  await page.setViewportSize({width:390,height:844});await modelForm.getByLabel("连接验证结果",{exact:true}).scrollIntoViewIfNeeded();await noOverflow();await capture("model-added-390");await modelForm.getByRole("button",{name:"开始聊天",exact:true}).click();await page.locator(".people-column").waitFor();
  await nav("设置");await page.locator(".up-connections").getByRole("button",{name:"修改",exact:true}).click();assert.equal(await page.getByLabel("API Key",{exact:true}).count(),0);await page.getByRole("button",{name:"更换 Key",exact:true}).click();assert.equal(await page.getByLabel("API Key",{exact:true}).inputValue(),"");await page.getByLabel("API Key",{exact:true}).fill("fixture-replacement-key-not-stored");validationOutcome="UPDATED";await page.getByRole("button",{name:"测试并保存",exact:true}).click();await page.getByRole("heading",{name:"已更新",exact:true}).waitFor();assert.equal(await page.getByLabel("API Key",{exact:true}).count(),0);await capture("model-updated-390");
  fixture="none";await nav("我");await page.reload();await page.getByRole("button",{name:"开始评估",exact:true}).click();await modelDialog.waitFor();await modelDialog.getByRole("button",{name:"稍后设置",exact:true}).click();
  record("模型验证夹具覆盖鉴权失败、测试通过但保存失败、已添加/已更新；Key在等待与失败期间保留，成功后展示安全状态，重新打开不回读；修改使旧结果失效、重复提交只发一次。");
  await page.getByRole("button",{name:"编辑个人资料",exact:true}).click();await page.getByLabel("昵称",{exact:true}).fill("测试用户");await page.getByLabel("自我介绍",{exact:true}).fill("隔离浏览器测试资料：喜欢清楚的证据，愿意保留反例。");await page.getByRole("button",{name:"保存资料",exact:true}).click();await page.getByRole("heading",{name:"测试用户",exact:true}).waitFor();
  await page.getByLabel("补充一条真实的自我陈述").fill("我在做重大选择前，总会先核查关键风险，也会记录反对自己的证据。");await page.getByLabel("这是我目前的真实观点，同意用作画像依据").check();await page.getByRole("button",{name:"确认加入资料",exact:true}).click();await page.waitForTimeout(250);
  const portrait=await readFile(path.resolve(here,"../public/portraits/munger.png"));await page.getByLabel("上传自定义头像").setInputFiles({name:"test-avatar.png",mimeType:"image/png",buffer:portrait});await page.waitForTimeout(250);assert.ok((await state()).upgrade.me.avatar_url.startsWith("/user-avatars/"));
  await page.getByRole("button",{name:"过往模拟记录",exact:false}).click();assert.equal(await page.getByRole("button",{name:"创建实验预览",exact:true}).count(),0);await hash("virtual");assert.ok(await page.getByRole("heading",{name:"让两种思考，慢慢相遇。"}).isVisible());assert.equal(await page.getByLabel("左侧人物",{exact:true}).locator('option').filter({hasText:"我的分身"}).count(),0);await hash("me");
  record("未配置的真实评估引导配置；本人资料、授权陈述和头像可保存；旧记录只读，虚拟空间可直接浏览，未评估分身不在参与者列表。");

  await page.setViewportSize({width:1440,height:1050});fixture="missing";await page.reload();await page.getByRole("button",{name:"开始评估",exact:true}).click();await modelDialog.waitFor();await modelDialog.getByRole("button",{name:"关闭模型配置",exact:true}).click();fixture="configured";await hash("chat");
  await choose("munger");
  const composer=page.getByLabel("写下你想聊的话",{exact:true});const beforeIMECount=await page.getByTestId("message-row").count();await composer.fill("浏览器隔离测试：中文输入法中不应发送。");await composer.dispatchEvent("compositionstart");await composer.press("Enter");assert.equal(await page.getByTestId("message-row").count(),beforeIMECount);await composer.dispatchEvent("compositionend");await page.getByRole("button",{name:"发送消息",exact:true}).click();await page.locator('[data-speaker="AGENT"]').first().waitFor();
  await composer.fill("芒格会话的私有草稿");await choose("buffett");assert.equal(await composer.inputValue(),"");await composer.fill("巴菲特会话的私有草稿");await choose("munger");assert.equal(await composer.inputValue(),"芒格会话的私有草稿");
  await page.getByRole("button",{name:"打开人物资料",exact:true}).click();await page.getByRole("button",{name:"移除人物",exact:true}).click();await confirm("移除人物");assert.equal(await page.getByRole("button",{name:"与查理·芒格聊天",exact:true}).count(),0);assert.equal((await state()).skills.find(item=>item.slug==="munger").status,"DELETED");
  await page.getByRole("button",{name:"添加人物",exact:true}).click();await page.getByRole("tab",{name:"内置人物目录",exact:true}).click();const munger=page.locator(".up-catalog article",{has:page.getByText("查理·芒格",{exact:true})});await munger.getByRole("button",{name:"重新添加",exact:true}).click();await page.getByRole("button",{name:"关闭人物管理",exact:true}).click();await page.getByRole("button",{name:"与查理·芒格聊天",exact:true}).waitFor();
  record("缺失会话Key引导设置；中文输入法、左右消息、独立草稿正常；删除人物保留历史并可从内置目录恢复。");

  await choose("munger");const formattedUser="[e2e_markdown] **用户原文保留** # 不改用户输入";await send(formattedUser);await reply();
  const prepared=page.locator('[data-speaker="AGENT"] .message-bubble').last();const preparedText=await prepared.innerText();assert.ok(!preparedText.includes("## "));assert.ok(!preparedText.includes("**别急着"));assert.ok(!preparedText.includes("```"));assert.ok(preparedText.includes("value_name = 2 * 3 ** 2"));assert.ok(preparedText.includes("# keep this comment"));assert.ok(preparedText.includes("https://example.com/source"));assert.ok(preparedText.includes("E = m * c^2"));assert.equal(await page.locator('[data-speaker="USER"] .message-bubble').last().innerText(),formattedUser);await capture("chat-prepared-reply-1440");await page.setViewportSize({width:390,height:844});await capture("chat-prepared-reply-390");await page.setViewportSize({width:1440,height:1050});
  record("新生成Markdown回复在保存前转为自然文本，代码注释、运算符、变量、公式和网址保留；用户原文不改。");
  await choose("munger");await send("[e2e_burst] 我想聊聊今天的小事。");await page.getByRole("status",{name:"对方正在输入中...",exact:true}).waitFor();
  await composer.fill("人物输入时，我也能继续补充");assert.equal(await page.getByRole("button",{name:"发送消息",exact:true}).isEnabled(),true);
  for(const width of [1440,768,390,360]){await page.setViewportSize({width,height:width>800?1050:844});await noOverflow();await capture(`chat-typing-${width}`);}
  await composer.fill("");await reply();assert.ok(await page.locator('.message-bubble',{hasText:"先说一句，今天这事还挺有意思。"}).isVisible());assert.ok(await page.locator('.message-bubble',{hasText:"刚才提到的那个细节，我倒是想多听听。"}).isVisible());assert.ok(await page.locator('.message-bubble',{hasText:"你当时为什么会这么想？"}).isVisible());
  await page.setViewportSize({width:1440,height:1050});await capture("chat-burst-1440");await page.setViewportSize({width:390,height:844});await capture("chat-burst-390");await page.setViewportSize({width:1440,height:1050});
  await send("[e2e_interrupt] 先说说原来的想法。");await page.locator('.message-bubble',{hasText:"这句已经发出了。"}).waitFor();await send("[e2e_revised] 我补充一下，重点其实变了。");await reply();await page.waitForTimeout(4500);assert.equal(await page.locator('.message-bubble',{hasText:"这句应该被插话取消。"}).count(),0);assert.equal(await page.locator('.message-bubble',{hasText:"这句也不应出现。"}).count(),0);assert.ok(await page.locator('.message-bubble',{hasText:"好，那就先说你补充的这一点。"}).isVisible());
  await send("[e2e_burst] 刷新后仍属于原来的会话。");await page.locator('.message-bubble',{hasText:"刷新后仍属于原来的会话。"}).waitFor();await page.reload();await choose("munger");await reply();assert.equal(await page.locator('.message-bubble',{hasText:"[e2e_burst] 刷新后仍属于原来的会话。"}).count(),1);
  record("聊天批次：标题输入动效、等待时继续输入、多气泡自然提问、中途插话清尾、刷新恢复均正确；四种宽度无横向溢出。");

  await choose("munger");const failedText=`[e2e_fail_once] 已保存的人类消息只应出现一次。${Date.now()}`;await send(failedText);await page.locator(".reply-failed").waitFor();const originalPost=messagePosts.at(-1);await page.getByRole("button",{name:"重试回复",exact:true}).click();await reply();assert.equal(await page.locator('.message-bubble',{hasText:failedText}).count(),1);assert.deepEqual(messagePosts.at(-1),originalPost);
  const slowText=`[e2e_wait] 这条等待中的回复属于芒格。${Date.now()}`;await send(slowText);await choose("buffett");await page.waitForTimeout(1500);assert.equal(await page.locator('.message-bubble',{hasText:slowText}).count(),0);await choose("munger");await reply();assert.equal(await page.locator('.message-bubble',{hasText:slowText}).count(),1);
  await send(Array.from({length:100},(_,index)=>`历史第${index+1}行：保留滚动位置和不同意见。`).join("\n"));await reply();await page.getByTestId("message-scroll").evaluate(element=>{element.scrollTop=140;element.dispatchEvent(new Event("scroll"));});const rememberedScroll=await page.getByTestId("message-scroll").evaluate(element=>element.scrollTop);await nav("我");await nav("聊天");assert.ok(Math.abs(await page.getByTestId("message-scroll").evaluate(element=>element.scrollTop)-rememberedScroll)<4);
  await send("[e2e_wait] 阅读历史时，出现新回复提示。");await page.waitForTimeout(250);await page.getByTestId("message-scroll").evaluate(element=>{element.scrollTop=120;element.dispatchEvent(new Event("scroll"));});await reply();await page.getByRole("button",{name:"查看新回复",exact:true}).waitFor();assert.ok(await page.getByTestId("message-scroll").evaluate(element=>element.scrollTop)<150);await page.getByRole("button",{name:"查看新回复",exact:true}).click();
  const rememberedText="我会为重要选择记录反例，这是可删除的浏览器证据。";await page.getByLabel("记住这条",{exact:true}).check();await send(rememberedText);await reply();assert.equal(await page.getByLabel("记住这条",{exact:true}).isChecked(),false);
  let releaseStaleUpdate,markStaleCaptured;const staleUpdateGate=new Promise(resolve=>{releaseStaleUpdate=resolve;});const staleCaptured=new Promise(resolve=>{markStaleCaptured=resolve;});let delayOneUpdate=true;
  const delayedUpdateHandler=async route=>{if(!delayOneUpdate)return route.continue();delayOneUpdate=false;const oldUrl=new URL(route.request().url());oldUrl.searchParams.set("after_sequence","0");const oldResponse=await route.fetch({url:oldUrl.toString()});const oldJson=await oldResponse.json();markStaleCaptured();await staleUpdateGate;await route.fulfill({response:oldResponse,json:oldJson});};
  await page.route("**/api/v1/real-conversations/*/updates?**",delayedUpdateHandler);await staleCaptured;
  await hash("mirror");const evidenceCard=page.locator(".evidence-card",{hasText:rememberedText});await evidenceCard.getByRole("button",{name:"删除",exact:true}).click();await confirm("删除证据");await nav("聊天");releaseStaleUpdate();await page.waitForTimeout(200);await page.unroute("**/api/v1/real-conversations/*/updates?**",delayedUpdateHandler);await choose("munger");assert.equal(await page.locator('.message-bubble',{hasText:rememberedText}).count(),0);await page.reload();await choose("munger");assert.equal(await page.locator('.message-bubble',{hasText:rememberedText}).count(),0);
  record("聊天回归：失败重试保留同一请求且不重复保存；切换人物时在途回复归属正确；历史滚动与新回复提示、单次记忆授权、删除缓存传播及迟到旧响应防复活正常。");

  await page.getByRole("button",{name:"添加人物",exact:true}).click();await page.getByLabel("人物名称",{exact:true}).fill("测试导师");await page.getByLabel("一句话介绍").fill("浏览器测试人物草稿。");await page.getByLabel("资料来源",{exact:true}).fill("自行编写的合成测试资料，版本1。");await page.getByLabel("许可与使用边界").fill("只作AI角色测试，不冒充真人，不调用工具。");await page.getByLabel("人物 Skill 资料").fill("# 测试导师\n关注问题背后的证据。表达简洁，承认不知道。不同意时解释理由，不执行工具或脚本。");await page.getByRole("button",{name:"保存待评草稿",exact:true}).click();await page.waitForTimeout(250);assert.equal(await page.getByRole("dialog",{name:"人物管理",exact:true}).getByRole("button",{name:"添加人物",exact:true}).count(),0);assert.ok((await state()).upgrade.skill_imports.some(item=>item.display_name==="测试导师"));await page.getByRole("button",{name:"关闭人物管理",exact:true}).click();
  record("导入资料先保存待评草稿，未评审不出现添加按钮；没有启动任何真实模型请求。");

  await page.getByRole("button",{name:"添加人物",exact:true}).click();
  const voiceFile=path.resolve(here,"../../artifacts/persona-imports/fengge-wangmingtianya/fengge.import.json");
  await page.getByLabel("导入 Skill 文件").setInputFiles(voiceFile);
  await page.waitForFunction(input=>input.value==="峰哥亡命天涯",await page.getByLabel("人物名称",{exact:true}).elementHandle());
  assert.equal(await page.getByLabel("人物名称",{exact:true}).inputValue(),"峰哥亡命天涯");
  assert.ok((await page.getByLabel("人物 Skill 资料").inputValue()).startsWith("WOT_VOICE_V1\n"));
  assert.ok((await page.getByLabel("资料来源",{exact:true}).inputValue()).includes("huggingface.co"));
  await page.getByRole("button",{name:"保存待评草稿",exact:true}).click();await page.waitForTimeout(250);
  assert.equal(await page.getByRole("dialog",{name:"人物管理",exact:true}).getByRole("button",{name:"添加人物",exact:true}).count(),0);
  assert.ok((await state()).upgrade.skill_imports.some(item=>item.display_name==="峰哥亡命天涯"&&item.status==="DRAFT"));
  await capture("fengge-voice-import-draft-1440");
  await page.getByRole("button",{name:"关闭人物管理",exact:true}).click();
  record("新版峰哥JSON可自动填入名称、来源与表达档案，保存为待评草稿；未评估不能添加，未发起模型调用。");

  const dimensions=[{id:"completeness",label:"资料完整性",score:62,threshold:70,issues:[{excerpt:"仅有一条规则",reason:"缺少例外情境和背景资料"}],suggestions:["补充三种具体情境及完整对话例子。"]},{id:"sources",label:"来源说明",score:78,threshold:70,issues:[],suggestions:[]},{id:"thinking_expression",label:"思维与表达规则",score:73,threshold:70,issues:[],suggestions:[]},{id:"consistency",label:"一致性",score:81,threshold:70,issues:[],suggestions:[]},{id:"permissions_honesty",label:"权限与诚实边界",score:90,threshold:70,issues:[],suggestions:[]}];
  fixtureTask={id:"fixture-quality",kind:"QUALITY",subject_type:"USER_SKILL",status:"NOT_PASSED",progress:{completed:1,total:1,percent:100},report:{title:"本人 Skill 资料质量评估",overall:76.8,passed:false,dimensions}};
  await page.evaluate(()=>{window.location.hash="me"});await page.getByRole("heading",{name:"测试用户",exact:true}).waitFor();
  // A task fixture is exposed only to this browser page, never stored or certified.
  await page.route("**/api/v1/assessments",async route=>{if(route.request().method()==="POST")await route.fulfill({json:fixtureTask});else await route.continue();});
  await page.route("**/api/v1/me/versions/*/assess",async route=>route.fulfill({json:fixtureTask}));await page.getByLabel("我确认资料描述我本人，同意用于本人开启的分身会话").check();await page.getByRole("button",{name:"开始评估",exact:true}).click();await page.getByRole("dialog",{name:"本人 Skill 资料质量评估",exact:true}).waitFor();assert.ok(await page.getByText("缺少例外情境和背景资料",{exact:false}).isVisible());assert.ok(await page.getByText("补充三种具体情境及完整对话例子。",{exact:false}).isVisible());await capture("quality-feedback-fixture-1440");await page.getByRole("button",{name:"关闭本人 Skill 资料质量评估",exact:true}).click();
  await page.locator(".up-strict-advanced > summary").click();
  fixtureTask={id:"fixture-personal",kind:"PERSONAL",status:"NEEDS_ANSWERS",stage:"本人作答",progress:{completed:7,total:254,percent:2.7559},questions:[{id:"q1",domain:"career",prompt:"你获得了一份收入更高但信息不足的工作机会。你会如何做决定？",options:{A:"先核查团队和风险，再决定",B:"迅速接受，机会优先",C:"保持现状，等待更多机会"}}],answer_status:[]};
  await page.getByRole("button",{name:"开始严格本人校验",exact:true}).click();await page.getByRole("dialog",{name:"严格个人校验",exact:true}).waitFor();assert.equal(await page.getByRole("progressbar").getAttribute("value"),"2.7559");await page.getByRole("radio").first().check();await page.getByLabel("你会这样选择的理由").fill("我会先看关键风险，再核查与预期不一致的证据。");await capture("heldout-answers-fixture-1440");await page.getByRole("button",{name:"关闭严格个人校验",exact:true}).click();
  fixtureTask={id:"fixture-incomplete",kind:"PERSONAL",status:"INCOMPLETE",stage:"四组预测",progress:{completed:50,total:254,percent:19.685},failure:{code:"ASSESSMENT_INCOMPLETE",message:"测试夹具：连接中断，评估尚未完成。"}};await page.getByRole("button",{name:"开始严格本人校验",exact:true}).click();await page.getByText("测试夹具：连接中断，评估尚未完成。",{exact:false}).waitFor();assert.ok(await page.getByRole("button",{name:"继续评估",exact:true}).isVisible());await page.getByRole("button",{name:"关闭严格个人校验",exact:true}).click();
  fixtureTask={id:"fixture-report",kind:"PERSONAL",status:"NOT_PASSED",progress:{completed:254,total:254,percent:100},report:{title:"严格个人校验报告",overall:82.125,passed:false,dimensions:dimensions.map(dimension=>({...dimension,threshold:75})),baselines:[{id:"B0",label:"无画像",overall:75.3},{id:"B1",label:"静态个人简介",overall:80.2},{id:"B2",label:"普通摘要/RAG",overall:83.125}],model:{provider:"openai",model:"visual-fixture-model"},sample_count:40,protocol:"strict-personal-v1",critical_checks:{forty_valid_answers:true,critical_safety:true,strictly_above_baselines:false},reasons:["用户Skill尚未严格优于最佳基线，请补充能区分个人习惯的具体情境。"]}};await page.getByRole("button",{name:"开始严格本人校验",exact:true}).click();await page.getByRole("dialog",{name:"严格个人校验",exact:true}).waitFor();assert.ok(await page.getByText("83.13",{exact:true}).isVisible());assert.equal(await page.getByText("NaN",{exact:false}).count(),0);assert.equal(await page.getByText("[object Object]",{exact:false}).count(),0);await capture("personal-report-fixture-1440");await page.setViewportSize({width:390,height:844});await capture("personal-report-fixture-390");await page.getByRole("button",{name:"关闭严格个人校验",exact:true}).click();
  record("浏览器专用评估夹具验证真实进度展示、低分原因片段与补充建议、保留题作答布局；不写入正式评估或许可。");

  for(const width of [1440,768,390,360]){
    await page.setViewportSize({width,height:width>760?1050:844});
    for(const route of ["chat","me","settings","mirror","chosen","growth","privacy","simulation"]){await hash(route);if(route==="chat")await choose("munger");await noOverflow();await capture(`${route}-${width}`);if(route==="chat"&&width<760){const composerBounds=await page.getByLabel("写下你想聊的话",{exact:true}).boundingBox();assert.ok(composerBounds&&composerBounds.y>=0&&composerBounds.y+composerBounds.height<=844);await page.getByRole("button",{name:"返回人物列表",exact:true}).click();await capture(`people-${width}`);}}
  }
  record("聊天、我、设置及旧画像/目标/成长/隐私/历史8页在1440/768/390/360px均无横向溢出。");

  fixture="virtual";fixtureSession=null;await page.setViewportSize({width:1440,height:1050});await hash("virtual");await page.getByLabel("左侧人物",{exact:true}).selectOption("fixture-self");await page.getByLabel("右侧人物",{exact:true}).selectOption("fixture-munger");await page.getByLabel("初始话题",{exact:true}).fill("面对一个回报很高，但缺少关键资料的机会，应该如何做决定？");
  for(const width of [1440,768,390,360]){await page.setViewportSize({width,height:width>760?1050:844});await noOverflow();await capture(`virtual-vs-setup-fixture-${width}`);}
  fixtureSession={id:"fixture-session",topic:"面对不确定的机会，如何做决定？",state:"RUNNING",participants:[{id:"fixture-self",version_id:"fixture-certified-v1",kind:"SELF",display_name:"测试用户",avatar_url:realState.upgrade.me.avatar_url},{id:"fixture-munger",version_id:"munger-v1",kind:"CHARACTER",display_name:"查理·芒格",avatar_url:"/portraits/munger.png"}],turns:[{id:"fixture-t1",speaker_id:"fixture-munger",display_name:"查理·芒格",avatar_url:"/portraits/munger.png",text:"先把问题反过来想：什么情况会让这个决定变得很糟？",turn_index:1,is_generated:true,version_id:"munger-v1"},{id:"fixture-t2",speaker_id:"fixture-self",display_name:"测试用户",avatar_url:realState.upgrade.me.avatar_url,text:"我会先核查最坏的结果，也想知道自己遗漏了哪些反对意见。",turn_index:2,is_generated:true,version_id:"fixture-certified-v1"}],draft:{id:"fixture-draft",speaker_id:"fixture-self",text:"与其一直等待确定性，我愿意先验证一个最小的假设。",publish_at:new Date(Date.now()+20000).toISOString()},used_tokens:1224,max_turns:20,max_tokens:30000,typing:false,revision:1};
  fixtureSession.turns.splice(1,0,...["再看一下代价落在谁身上。","这个风险你愿意承受多久？"].map((text,index)=>({...fixtureSession.turns[0],id:`fixture-burst-${index}`,text,turn_index:index+2,generation_id:"fixture-generation",part_index:index+1})));
  await page.reload();await page.getByLabel("切换虚拟会话",{exact:true}).selectOption("fixture-session");await page.getByRole("button",{name:"接管这一句",exact:true}).waitFor();
  for(const width of [1440,768,390,360]){await page.setViewportSize({width,height:width>760?1050:844});await noOverflow();await capture(`virtual-conversation-fixture-${width}`);}
  const savedDraft=fixtureSession.draft;
  fixtureSession={...fixtureSession,typing:true,typing_speaker_id:"fixture-munger",typing_display_name:"查理·芒格",draft:{id:"fixture-hidden-character-draft",speaker_id:"fixture-munger",text:"不应提前可见的角色草稿"}};
  await page.getByRole("status",{name:"对方正在输入中...",exact:true}).waitFor();assert.equal(await page.getByRole("button",{name:"接管这一句",exact:true}).count(),0);assert.equal(await page.getByText("不应提前可见的角色草稿",{exact:true}).count(),0);assert.equal(await page.locator(".up-virtual-header .up-badge").innerText(),"AI 模拟");
  assert.deepEqual(await page.locator(".up-virtual-turn header strong").allTextContents(),["查理·芒格","查理·芒格","查理·芒格","测试用户"]);
  for(const width of [1440,768,390,360]){await page.setViewportSize({width,height:width>760?1050:844});await noOverflow();await capture(`virtual-typing-fixture-${width}`);}
  fixtureSession={...fixtureSession,typing:false,draft:savedDraft};await page.getByRole("button",{name:"接管这一句",exact:true}).waitFor();
  await page.getByRole("button",{name:"接管这一句",exact:true}).click();await page.getByLabel("改写分身发言").waitFor();await page.getByRole("radio",{name:"这是我的真实观点",exact:true}).check();assert.equal(await page.getByRole("button",{name:"保存干预",exact:true}).isDisabled(),true);await capture("virtual-intervention-fixture-360");
  record("VS双人物选择、同一人物连续气泡、标题输入动效、角色待发内容隐藏、草稿干预与真实观点确认在4种宽度展示正确；全部为页面夹具，后端未写入任何本人分身准入报告。");
  assert.equal((await state()).privacy.external_calls,0);
  const configureFixture=await context.request.post(`${base}/api/v1/e2e/virtual-transport`,{headers:{"X-WOT-Token":sessionToken},data:{}});assert.equal(configureFixture.ok(),true);
  fixture="none";fixtureSession=null;await page.reload();await nav("虚拟空间");
  assert.equal((await state()).upgrade.me.self_admission.eligible,false);assert.equal((await state()).upgrade.unlock.unlocked,false);
  assert.equal(await page.getByLabel("左侧人物",{exact:true}).locator("option").filter({hasText:"我的分身"}).count(),0);
  await page.getByLabel("左侧人物",{exact:true}).selectOption("skill_munger");await page.getByLabel("右侧人物",{exact:true}).selectOption("skill_buffett");await page.getByLabel("初始话题",{exact:true}).fill("隔离浏览器测试：两位人物无需本人评估即可持续交流。");await page.getByLabel("最多发言",{exact:false}).fill("2");await page.getByLabel("同意双方自动分享本场生成发言，仅在本场会话内可见").check();await page.getByRole("button",{name:"开始对话",exact:true}).click();
  await page.getByRole("heading",{name:"本轮消息额度已用完",exact:true}).waitFor();let pureSession=(await state()).upgrade.virtual_sessions[0];assert.equal(pureSession.state,"PAUSED_LIMIT");assert.equal(pureSession.turns.length,2);
  for(const width of [1440,768,390,360]){await page.setViewportSize({width,height:width>760?1050:844});await noOverflow();await capture(`virtual-budget-pause-${width}`);}
  const transportCount=async()=>(await context.request.post(`${base}/api/v1/e2e/transport-count`,{headers:{"X-WOT-Token":sessionToken},data:{}})).json();const pausedCalls=(await transportCount()).mock_transport_calls;assert.equal(typeof pausedCalls,"number");assert.ok(pausedCalls>0);
  await page.getByLabel("新增消息数",{exact:true}).fill("2");await page.getByLabel("新增 token",{exact:true}).fill("0");await page.getByRole("button",{name:"追加额度",exact:true}).click();await page.getByRole("heading",{name:"额度已补充，点击继续接着聊",exact:true}).waitFor();await page.waitForTimeout(400);assert.equal((await transportCount()).mock_transport_calls,pausedCalls);assert.equal((await state()).upgrade.virtual_sessions[0].state,"PAUSED_LIMIT");
  await page.getByRole("button",{name:"继续",exact:true}).click();pureSession=await waitVirtual(value=>value.turns.length===4&&value.state==="PAUSED_LIMIT");assert.equal(pureSession.state,"PAUSED_LIMIT");assert.equal(new Set(pureSession.turns.map(turn=>turn.speaker_id)).size,2);assert.equal(new Set(pureSession.turns.map(turn=>turn.id)).size,4);const completedCalls=(await transportCount()).mock_transport_calls;await page.waitForTimeout(400);assert.equal((await transportCount()).mock_transport_calls,completedCalls);
  const forgedSelf=await context.request.post(`${base}/api/v1/virtual-sessions`,{headers:{"X-WOT-Token":sessionToken,"Idempotency-Key":"e2e-unassessed-self"},data:{left_id:"self:unassessed-profile",right_id:"skill_munger",topic:"不可绕过本人资料评估",sharing_approved:true,start_immediately:true}});assert.equal(forgedSelf.ok(),false);
  record("未评估用户可通过真实界面一键启动纯人物会话；两个独立人物接续发言，到限暂停，追加额度不自动生成，继续后保留原历史；未评估SELF直接请求仍被拒绝。模型传输使用进程内夹具，真实调用为0。");
  assert.equal((await state()).upgrade.unlock.unlocked,false);assert.deepEqual(errors,[]);assert.deepEqual(external,[]);for(const secret of ["fixture-only-key-never-saved","fixture-replacement-key-not-stored"]){assert.ok(!JSON.stringify(await context.storageState()).includes(secret));assert.ok(!await page.evaluate(value=>JSON.stringify({...localStorage,...sessionStorage}).includes(value),secret));}
  await writeFile(path.join(artifacts,"report.json"),JSON.stringify({status:"PASSED",date:new Date().toISOString(),checks,screenshots,browserErrors:errors,externalRequests:external,externalModelCalls:0,mockTransportCalls:(await transportCount()).mock_transport_calls,limitations:["评估和本人分身画面使用浏览器夹具，不授予正式准入；纯人物创建/启动/额度恢复则走实际后端和进程内模拟传输。","真实评估与后台调度另有独立后端测试；所有模型响应来自离线或进程内模拟传输，未调用真实供应商。","所有写入仅发生在隔离 e2e 数据目录。"]},null,2));
}catch(error){await capture("failure").catch(()=>{});await writeFile(path.join(artifacts,"report.json"),JSON.stringify({status:"FAILED",checks,screenshots,browserErrors:errors,error:String(error)},null,2));throw error;}
finally{await browser.close();}
