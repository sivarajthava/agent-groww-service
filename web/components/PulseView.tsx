import ReactMarkdown from "react-markdown";

/** The validated pulse.md exactly as published. Raw HTML in the Markdown is not rendered. */
export function PulseView({ markdown }: { markdown: string }) {
  return (
    <div className="pulse">
      <ReactMarkdown skipHtml>{markdown}</ReactMarkdown>
    </div>
  );
}
