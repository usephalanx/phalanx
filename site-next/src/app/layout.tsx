import type { Metadata } from 'next';
import { Inter, JetBrains_Mono, Space_Grotesk } from 'next/font/google';
import './globals.css';

const inter = Inter({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700', '800', '900'],
  variable: '--font-inter',
  display: 'swap',
});

const spaceGrotesk = Space_Grotesk({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  variable: '--font-space-grotesk',
  display: 'swap',
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-jetbrains',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'Phalanx — From Slack Command to Shipped Software',
  description:
    'Open-source AI team operating system. Specialized agents coordinate from planning to production with human approval at every gate. Config-driven. Self-hostable.',
  openGraph: {
    title: 'Phalanx — From Slack Command to Shipped Software',
    description:
      'Specialized AI agents ship your code in formation. Human-approved at every gate. Open source.',
    url: 'https://usephalanx.com',
    type: 'website',
    siteName: 'Phalanx',
  },
  twitter: {
    card: 'summary_large_image',
    site: '@usephalanx',
    title: 'Phalanx — From Slack Command to Shipped Software',
    description:
      'Agents in formation. You command. Open source AI team operating system.',
  },
  metadataBase: new URL('https://usephalanx.com'),
  alternates: {
    canonical: '/',
  },
};

/**
 * Root layout for the Phalanx marketing site.
 * Loads Inter, Space Grotesk, and JetBrains Mono via next/font/google.
 * Applies dark background, base text color, and font antialiasing.
 */
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${spaceGrotesk.variable} ${jetbrainsMono.variable}`}
    >
      <body className="bg-bg text-text font-sans antialiased leading-relaxed">
        {children}
      </body>
    </html>
  );
}
