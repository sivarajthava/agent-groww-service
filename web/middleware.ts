import { NextResponse, type NextRequest } from "next/server";

// Optional HTTP Basic auth for a public deployment: set WEB_BASIC_AUTH="user:password" in Vercel.
// Unset (local dev), every request passes. Vercel Deployment Protection is the alternative on paid plans.
export function middleware(req: NextRequest) {
  const expected = process.env.WEB_BASIC_AUTH;
  if (!expected) return NextResponse.next();

  const [scheme, encoded] = (req.headers.get("authorization") ?? "").split(" ");
  let given = "";
  try {
    given = scheme === "Basic" && encoded ? atob(encoded) : "";
  } catch {
    given = "";
  }
  if (safeEqual(given, expected)) return NextResponse.next();

  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Groww Weekly Pulse", charset="UTF-8"' },
  });
}

function safeEqual(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  for (let i = 0; i < b.length; i++) diff |= (a.charCodeAt(i) || 0) ^ b.charCodeAt(i);
  return diff === 0;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
