export function TypingIndicator() {
  return <span className="chat-typing" role="status" aria-label="对方正在输入中..."><span>对方正在输入中...</span><span className="typing-dots" aria-hidden="true"><i/><i/><i/></span></span>;
}
