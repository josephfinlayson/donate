"use client";

import { useEffect, useRef, useState } from "react";
import { io, Socket } from "socket.io-client";

interface Message {
  role: "user" | "bot";
  content: string;
  checkoutUrl?: string;
}

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8080";

export function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isConnected, setIsConnected] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const socketRef = useRef<Socket | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const socket = io(BACKEND_URL, { transports: ["websocket", "polling"] });
    socketRef.current = socket;

    socket.on("connect", () => {
      setIsConnected(true);
      socket.emit("start_session", {});
    });

    socket.on("session_started", (data: { session_id: string; message: string }) => {
      setMessages([{ role: "bot", content: data.message }]);
      setIsLoading(false);
    });

    socket.on("bot_message", (data: { message: string; checkout_url?: string }) => {
      setMessages((prev) => [
        ...prev,
        { role: "bot", content: data.message, checkoutUrl: data.checkout_url },
      ]);
      setIsLoading(false);
    });

    socket.on("error", (data: { message: string }) => {
      console.error("Socket error:", data.message);
      setIsLoading(false);
    });

    socket.on("disconnect", () => {
      setIsConnected(false);
    });

    return () => {
      socket.disconnect();
    };
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const sendMessage = () => {
    const text = input.trim();
    if (!text || !socketRef.current || isLoading) return;

    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInput("");
    setIsLoading(true);
    socketRef.current.emit("send_message", { message: text });
  };

  return (
    <div className="bg-white rounded-2xl shadow-lg border border-stone-200 flex flex-col h-[600px]">
      {/* Header */}
      <div className="px-6 py-4 border-b border-stone-100">
        <h1 className="font-semibold text-lg">GiveDirectly</h1>
        <p className="text-sm text-stone-500">
          Chat with us about making an impact
        </p>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {messages.map((msg, i) => (
          <div key={i}>
            <div
              className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
                  msg.role === "user"
                    ? "bg-emerald-600 text-white"
                    : "bg-stone-100 text-stone-800"
                }`}
              >
                {msg.content}
              </div>
            </div>
            {msg.checkoutUrl && (
              <div className="flex justify-start mt-2">
                <a
                  href={msg.checkoutUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={() => socketRef.current?.emit("link_clicked", {})}
                  className="inline-flex items-center gap-2 px-4 py-2 bg-emerald-50 text-emerald-700 border border-emerald-200 rounded-xl text-sm font-medium hover:bg-emerald-100 transition"
                >
                  💚 Donate now
                </a>
              </div>
            )}
          </div>
        ))}

        {isLoading && messages.length > 0 && (
          <div className="flex justify-start">
            <div className="bg-stone-100 rounded-2xl px-4 py-2.5 text-sm text-stone-400">
              <span className="animate-pulse">Thinking...</span>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="px-4 py-3 border-t border-stone-100">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            sendMessage();
          }}
          className="flex gap-2"
        >
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={isConnected ? "Type a message..." : "Connecting..."}
            disabled={!isConnected || isLoading}
            className="flex-1 px-4 py-2.5 bg-stone-50 border border-stone-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-transparent disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={!isConnected || isLoading || !input.trim()}
            className="px-4 py-2.5 bg-emerald-600 text-white rounded-xl text-sm font-medium hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed transition"
          >
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
