"use client";

import { useEffect, useState, useCallback } from "react";

const API = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8080";

interface Stats {
  total_sessions: number;
  completed_sessions: number;
  donations: number;
  donation_rate: number;
  total_donated_usd: number;
  avg_composite_score: number | null;
  prompt_versions: {
    version: string;
    sessions: number;
    avg_score: number | null;
    donations: number;
  }[];
  sessions_since_last_optimization: number;
  optimization_threshold: number;
}

interface FunnelStats {
  total: number;
  funnel: {
    sessions: number;
    asked_about_charity: { count: number; rate: number };
    payment_link_shown: { count: number; rate: number };
    clicked_payment_link: { count: number; rate: number };
    started_checkout: { count: number; rate: number };
    completed_payment: { count: number; rate: number };
  };
}

interface OptRun {
  id: string;
  prompt_version_before: string;
  prompt_version_after: string | null;
  sessions_count: number;
  trigger_reason: string;
  status: string;
  deployed: boolean;
  created_at: string;
}

interface Reflection {
  timestamp: string;
  reflections?: string;
  decision?: string;
  metrics?: Record<string, unknown>;
}

interface PromptVersion {
  version: string;
  parent_version?: string;
  created_at?: string;
  created_by?: string;
  instructions_preview: string;
}

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [funnel, setFunnel] = useState<FunnelStats | null>(null);
  const [optRuns, setOptRuns] = useState<OptRun[]>([]);
  const [reflections, setReflections] = useState<Reflection[]>([]);
  const [promptVersions, setPromptVersions] = useState<PromptVersion[]>([]);
  const [optimizing, setOptimizing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    try {
      const [statsRes, funnelRes, runsRes, reflRes, promptRes] = await Promise.all([
        fetch(`${API}/api/stats`),
        fetch(`${API}/api/funnel-stats`),
        fetch(`${API}/api/optimization-runs`),
        fetch(`${API}/api/gepa-reflections`),
        fetch(`${API}/api/prompt-history`),
      ]);

      setStats(await statsRes.json());
      setFunnel(await funnelRes.json());
      setOptRuns((await runsRes.json()).runs);
      setReflections((await reflRes.json()).reflections);
      setPromptVersions((await promptRes.json()).versions);
      setError(null);
    } catch (e) {
      setError(`Failed to load: ${e}`);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    const interval = setInterval(fetchAll, 15000);
    return () => clearInterval(interval);
  }, [fetchAll]);

  const triggerOptimization = async () => {
    setOptimizing(true);
    try {
      const res = await fetch(`${API}/api/optimize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "manual_dashboard" }),
      });
      const result = await res.json();
      alert(
        result.status === "completed"
          ? `Optimization complete! ${result.deployed ? "Deployed " + result.version_after : "No change deployed."}`
          : `Optimization ${result.status}: ${result.reason || result.error || ""}`
      );
      fetchAll();
    } catch (e) {
      alert(`Optimization failed: ${e}`);
    } finally {
      setOptimizing(false);
    }
  };

  if (!stats) {
    return (
      <main className="min-h-dvh flex items-center justify-center p-4">
        <p className="text-stone-500">Loading dashboard...</p>
      </main>
    );
  }

  return (
    <main className="min-h-dvh bg-stone-50 p-6">
      <div className="max-w-6xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">Donation Chatbot Dashboard</h1>
            <p className="text-sm text-stone-500">Monitoring & A/B Testing</p>
          </div>
          <div className="flex gap-3">
            <button
              onClick={fetchAll}
              className="px-3 py-1.5 text-sm bg-stone-200 rounded-lg hover:bg-stone-300 transition"
            >
              Refresh
            </button>
            <button
              onClick={triggerOptimization}
              disabled={optimizing}
              className="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 transition"
            >
              {optimizing ? "Running GEPA..." : "Run Optimization"}
            </button>
          </div>
        </div>

        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-2 rounded-lg text-sm">
            {error}
          </div>
        )}

        {/* KPI Cards */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Card label="Total Sessions" value={stats.total_sessions} />
          <Card label="Completed" value={stats.completed_sessions} />
          <Card
            label="Donations"
            value={stats.donations}
            sub={`${(stats.donation_rate * 100).toFixed(1)}% rate`}
          />
          <Card
            label="Total Donated"
            value={`$${stats.total_donated_usd.toFixed(2)}`}
          />
          <Card
            label="Avg Composite Score"
            value={stats.avg_composite_score?.toFixed(2) ?? "—"}
          />
          <Card
            label="Sessions Since Opt"
            value={stats.sessions_since_last_optimization}
            sub={`threshold: ${stats.optimization_threshold}`}
          />
          <Card
            label="Active Versions"
            value={stats.prompt_versions.length}
          />
          <Card
            label="Current Version"
            value={stats.prompt_versions[0]?.version ?? "—"}
          />
        </div>

        {/* Funnel */}
        {funnel && funnel.total > 0 && (
          <section className="bg-white rounded-xl border border-stone-200 p-5">
            <h2 className="font-semibold mb-4">Conversion Funnel</h2>
            <div className="space-y-2">
              {[
                { label: "Sessions", ...funnel.funnel.asked_about_charity, count: funnel.total, rate: 1 },
                { label: "Asked About Charity", ...funnel.funnel.asked_about_charity },
                { label: "Payment Link Shown", ...funnel.funnel.payment_link_shown },
                { label: "Clicked Link", ...funnel.funnel.clicked_payment_link },
                { label: "Started Checkout", ...funnel.funnel.started_checkout },
                { label: "Completed Payment", ...funnel.funnel.completed_payment },
              ].map((step, i) => (
                <div key={i} className="flex items-center gap-3">
                  <span className="w-44 text-sm text-stone-600">{step.label}</span>
                  <div className="flex-1 bg-stone-100 rounded-full h-6 relative overflow-hidden">
                    <div
                      className="bg-emerald-500 h-full rounded-full transition-all"
                      style={{ width: `${Math.max(step.rate * 100, 1)}%` }}
                    />
                    <span className="absolute inset-0 flex items-center justify-center text-xs font-medium">
                      {step.count} ({(step.rate * 100).toFixed(1)}%)
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Prompt Version Comparison */}
        {stats.prompt_versions.length > 0 && (
          <section className="bg-white rounded-xl border border-stone-200 p-5">
            <h2 className="font-semibold mb-4">Prompt Version Performance</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-stone-500 border-b border-stone-100">
                    <th className="pb-2 pr-4">Version</th>
                    <th className="pb-2 pr-4">Sessions</th>
                    <th className="pb-2 pr-4">Avg Score</th>
                    <th className="pb-2 pr-4">Donations</th>
                    <th className="pb-2">Donation Rate</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.prompt_versions.map((v) => (
                    <tr key={v.version} className="border-b border-stone-50">
                      <td className="py-2 pr-4 font-mono text-xs">{v.version}</td>
                      <td className="py-2 pr-4">{v.sessions}</td>
                      <td className="py-2 pr-4">{v.avg_score?.toFixed(2) ?? "—"}</td>
                      <td className="py-2 pr-4">{v.donations}</td>
                      <td className="py-2">
                        {v.sessions > 0
                          ? `${((v.donations / v.sessions) * 100).toFixed(1)}%`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* Prompt History */}
        {promptVersions.length > 0 && (
          <section className="bg-white rounded-xl border border-stone-200 p-5">
            <h2 className="font-semibold mb-4">Prompt Evolution History</h2>
            <div className="space-y-3">
              {promptVersions.map((v) => (
                <div
                  key={v.version}
                  className="border border-stone-100 rounded-lg p-3"
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-mono text-xs bg-stone-100 px-2 py-0.5 rounded">
                      {v.version}
                    </span>
                    {v.parent_version && (
                      <span className="text-xs text-stone-400">
                        from {v.parent_version}
                      </span>
                    )}
                    <span className="text-xs text-stone-400">
                      {v.created_by}
                    </span>
                  </div>
                  <p className="text-xs text-stone-600 line-clamp-2">
                    {v.instructions_preview}...
                  </p>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Optimization Runs */}
        {optRuns.length > 0 && (
          <section className="bg-white rounded-xl border border-stone-200 p-5">
            <h2 className="font-semibold mb-4">Optimization Runs</h2>
            <div className="space-y-2">
              {optRuns.map((run) => (
                <div
                  key={run.id}
                  className="flex items-center gap-3 border border-stone-100 rounded-lg p-3 text-sm"
                >
                  <span
                    className={`w-2 h-2 rounded-full flex-shrink-0 ${
                      run.deployed ? "bg-emerald-500" : "bg-stone-300"
                    }`}
                  />
                  <span className="font-mono text-xs">
                    {run.prompt_version_before} → {run.prompt_version_after ?? "—"}
                  </span>
                  <span className="text-stone-400 text-xs">
                    {run.sessions_count} sessions
                  </span>
                  <span className="text-stone-400 text-xs">{run.trigger_reason}</span>
                  <span className="text-stone-400 text-xs ml-auto">
                    {new Date(run.created_at).toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* GEPA Reflections */}
        {reflections.length > 0 && (
          <section className="bg-white rounded-xl border border-stone-200 p-5">
            <h2 className="font-semibold mb-4">GEPA Reflections</h2>
            <div className="space-y-4">
              {reflections.map((r) => (
                <div
                  key={r.timestamp}
                  className="border border-stone-100 rounded-lg p-4"
                >
                  <div className="flex items-center gap-2 mb-2">
                    <span className="font-mono text-xs bg-stone-100 px-2 py-0.5 rounded">
                      {r.timestamp}
                    </span>
                  </div>
                  {r.reflections && (
                    <div className="text-xs text-stone-600 whitespace-pre-wrap mb-2 max-h-40 overflow-y-auto">
                      {r.reflections}
                    </div>
                  )}
                  {r.decision && (
                    <details className="text-xs text-stone-500">
                      <summary className="cursor-pointer hover:text-stone-700">
                        Decision reasoning
                      </summary>
                      <pre className="mt-1 whitespace-pre-wrap">{r.decision}</pre>
                    </details>
                  )}
                </div>
              ))}
            </div>
          </section>
        )}
      </div>
    </main>
  );
}

function Card({
  label,
  value,
  sub,
}: {
  label: string;
  value: string | number;
  sub?: string;
}) {
  return (
    <div className="bg-white rounded-xl border border-stone-200 p-4">
      <p className="text-xs text-stone-500 mb-1">{label}</p>
      <p className="text-xl font-semibold">{value}</p>
      {sub && <p className="text-xs text-stone-400 mt-0.5">{sub}</p>}
    </div>
  );
}
