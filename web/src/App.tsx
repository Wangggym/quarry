import Header from "./Header";
import ResultWorkbench from "./ResultWorkbench";
import { useToastStore } from "./store/toastStore";

function Toast() {
  const msg = useToastStore((s) => s.msg);
  const ok = useToastStore((s) => s.ok);
  if (msg === null) return null;
  return (
    <div
      id="toast"
      className="toast"
      style={{
        background: ok ? "var(--ok-bg)" : "var(--red-bg)",
        color: ok ? "var(--ok)" : "var(--red-fg)",
        borderColor: ok ? "var(--ok)" : "var(--red)",
      }}
    >
      {msg}
    </div>
  );
}

/** App shell: header bar on top, the workbench (sidebar + query section)
 * filling the rest — the legacy GUI's `<body>` layout. */
export default function App() {
  return (
    <>
      <Header />
      <ResultWorkbench />
      <Toast />
    </>
  );
}
