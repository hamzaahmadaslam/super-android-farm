import { notFound } from 'next/navigation';
import { pageBySlug, pages } from '../../../lib/docs.mjs';
import { DocPage } from '../shell.jsx';

export const dynamicParams = false;

export function generateStaticParams() {
  return pages.filter((page) => page.slug).map((page) => ({ slug: page.slug }));
}

export async function generateMetadata({ params }) {
  const page = pageBySlug((await params).slug);
  return page ? { title: page.title } : {};
}

export default async function Doc({ params }) {
  const page = pageBySlug((await params).slug);
  if (!page) notFound();
  return <DocPage page={page} />;
}
