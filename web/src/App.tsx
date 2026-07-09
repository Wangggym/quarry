import { useEffect, useState } from "react";

type VersionInfo = { name: string; version: string };

export default function App() {
  const [info, setInfo] = useState<VersionInfo | null>(null);

  useEffect(() => {
    fetch("/api/version")
      .then((r) => r.json())
      .then(setInfo)
      .catch(() => setInfo({ name: "Quarry", version: "?" }));
  }, []);

  return (
    <main className="app">
      <h1>{info?.name ?? "Quarry"}</h1>
      {info && <p className="version">v{info.version}</p>}
    </main>
  );
}
