import { ClosingCta } from "@/components/landing/ClosingCta";
import { Features } from "@/components/landing/Features";
import { Footer } from "@/components/landing/Footer";
import { Hero } from "@/components/landing/Hero";
import { HowItWorks } from "@/components/landing/HowItWorks";
import { TopBar } from "@/components/landing/TopBar";

export default function HomePage() {
  return (
    <>
      <TopBar />
      <main>
        <Hero />
        <Features />
        <HowItWorks />
        <ClosingCta />
      </main>
      <Footer />
    </>
  );
}
