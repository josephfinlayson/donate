import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GiveDirectly — Make a Difference",
  description: "Chat with us about making an impact through direct giving.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-stone-50 text-stone-900 antialiased">{children}</body>
    </html>
  );
}
