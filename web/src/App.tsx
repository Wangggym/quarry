import { useEffect, useState } from "react";
import { fetchVersion, type VersionInfo } from "./api";
import ResultWorkbench from "./ResultWorkbench";

export default function App() {
  const [info, setInfo] = useState<VersionInfo | null>(null);

  useEffect(() => {
    fetchVersion()
      .then(setInfo)
      .catch(() => setInfo({ name: "Quarry", version: "?" }));
  }, []);

  return (
    <main className="app">
      <header className="app-header">
        <h1>{info?.name ?? "Quarry"}</h1>
        {info && <p className="version">v{info.version}</p>}
      </header>
      <ResultWorkbench />
    </main>
  );
}
