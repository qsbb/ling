import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';
// 外壳样式放在 styles.css 之后：它要按 token 覆写 `.sidebar` 的浮层级
// （`styles.css` 里是写死的 z-index: 40），同优先级的规则靠加载顺序生效。
import './nav.css';
// 语音消息（SP03）：播放控件与「语音标签 + 文字」的样式。只作用于
// `.voice-message*` 自己的类，不覆写别处，所以放在最后没有顺序风险。
import './voice.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('缺少 #root 挂载点');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
