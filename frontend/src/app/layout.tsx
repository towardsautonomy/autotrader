import type { Metadata, Viewport } from "next";
import "./globals.css";
import ModeBanner from "@/components/ModeBanner";
import NavBar from "@/components/NavBar";

export const metadata: Metadata = {
  title: "autotrader // paper-first",
  description: "AI auto-trader — paper-first",
  // iOS Safari rewrites number/date-looking spans into <a> tags after
  // hydration, which React 19 reports as a hydration mismatch. We print
  // a lot of numbers ("12,345 tok", "$0.0289") so opt out globally.
  formatDetection: {
    telephone: false,
    date: false,
    address: false,
    email: false,
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#0a0f14",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    // suppressHydrationWarning on <html> covers browser / iOS Safari
    // attribute injection (theme, class mutation, etc.); on <body> covers
    // content-injecting extensions. Footer text also gets the flag because
    // iOS can wrap version-shaped strings ("v0.1.0") in <a> tags post-
    // hydration even with format-detection meta set.
    <html lang="en" className="h-full antialiased" suppressHydrationWarning>
      <body
        className="min-h-screen bg-bg text-text font-mono"
        suppressHydrationWarning
      >
        <ModeBanner />
        <NavBar />
        <main className="px-4 sm:px-6 py-6 max-w-6xl mx-auto relative z-10">
          {children}
        </main>
        <footer
          className="max-w-6xl mx-auto px-4 sm:px-6 pb-8 text-xs text-text-faint relative z-10"
          suppressHydrationWarning
        >
          <span className="text-accent">$</span> autotrader · bounded-loss · risk-engine-enforced · <span className="blink text-accent">▊</span>
        </footer>
      </body>
    </html>
  );
}
