import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "Groww Weekly Pulse",
  description: "Weekly Play Store review pulse for Groww: themes, verbatim quotes, action ideas and run health.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="topbar">
            <Link href="/" className="brand">
              <span className="brand-mark" aria-hidden="true">G</span>
              Groww Weekly Pulse
            </Link>
            <span className="topbar-note">Play Store reviews · read-only</span>
          </header>
          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}
