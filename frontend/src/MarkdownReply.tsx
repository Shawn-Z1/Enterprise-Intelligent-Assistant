import Markdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

// 仅调整链接、图片和宽表格的展示，保留解析器默认的安全URL处理，不解释原始HTML。
const components: Components = {
  /** 接收解析后的链接与文本，在新标签页打开有效链接；无有效地址时保留文字。 */
  a({ href, children, title }) {
    return href ? <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>;
  },
  /** 接收图片描述和地址，展示为用户主动打开的链接，避免聊天渲染自动请求第三方图片。 */
  img({ src, alt }) {
    return typeof src==='string' && src ? <a href={src} target="_blank" rel="noopener noreferrer">{alt || '图片链接'}</a> : <span>{alt || '图片'}</span>;
  },
  /** 接收表格内容，使用独立横向滚动容器，避免宽表格撑开聊天区和小屏页面。 */
  table({ children }) {
    return <div className="markdown-table"><table>{children}</table></div>;
  },
};

/** 接收模型原始回复字符串，渲染标题、强调、列表、引用、代码和GFM表格；不修改持久消息内容。 */
export function MarkdownReply({ content }: { content: string }) {
  return <div className="message-content message-markdown"><Markdown remarkPlugins={[remarkGfm]} components={components} skipHtml>{content}</Markdown></div>;
}
