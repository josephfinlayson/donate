"use client";

export default function Thanks() {
  return (
    <main className="min-h-dvh flex items-center justify-center p-4">
      <div className="max-w-md text-center space-y-4">
        <h1 className="text-3xl font-bold text-emerald-700">Thank you!</h1>
        <p className="text-stone-600 text-lg">
          Your donation to GiveDirectly will make a real difference. Cash
          transfers let families decide what they need most — and the evidence
          shows it works.
        </p>
        <a
          href="/"
          className="inline-block mt-4 px-6 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition"
        >
          Back to chat
        </a>
      </div>
    </main>
  );
}
