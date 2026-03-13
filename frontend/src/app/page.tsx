"use client";

import { Chat } from "@/components/Chat";

export default function Home() {
  return (
    <main className="min-h-dvh flex items-center justify-center p-4">
      <div className="w-full max-w-2xl">
        <Chat />
      </div>
    </main>
  );
}
