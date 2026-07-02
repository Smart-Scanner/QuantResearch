import { test, expect, Page } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

// Load registered routes to check coverage
let registeredRoutes: string[] = [];
try {
  registeredRoutes = JSON.parse(fs.readFileSync('tests/registered_routes.json', 'utf8'));
} catch (e) {
  console.log("Could not load registered_routes.json", e);
}

const testedRoutes = new Set<string>();

const ROUTES_TO_TEST = [
  '/',
  '/hc',
  '/top-picks',
  '/golden',
  '/breakouts',
  '/discovery',
  '/research',
  '/market',
  '/portfolio',
  '/watchlist',
  '/settings',
  '/mission-control',
  '/about',
  '/contact',
  '/symbol/RELIANCE'
];

// Helper to wait and capture
async function captureStates(page: Page, route: string, role: string, suffix = '') {
  // wait for network idle to ensure data loaded
  await page.waitForLoadState('networkidle');
  
  const safeRoute = route === '/' ? 'home' : route.replace(/\//g, '_').replace(/^_/, '');
  const dir = `audit_baseline/${test.info().project.name.toLowerCase().replace(' ', '_')}`;
  
  await page.screenshot({ path: `${dir}/${role}_${safeRoute}${suffix}_full.png`, fullPage: true });
  await page.screenshot({ path: `${dir}/${role}_${safeRoute}${suffix}_hero.png` });
  
  // Mobile fold depends on viewport, playwright takes visible viewport by default
}

async function loginAs(page: Page, role: 'admin' | 'testuser') {
  await page.goto('/auth/local-login');
  await page.fill('input[name="username"]', role);
  await page.fill('input[name="password"]', 'admin123');
  await page.click('button[type="submit"]');
  await page.waitForLoadState('networkidle');
}

test.describe('UI Freeze Certification Audit', () => {

  const metrics: any[] = [];
  let p0_count = 0;
  let p1_count = 0;
  let p2_count = 0;
  
  test.beforeEach(async ({ page }) => {
    page.on('pageerror', err => {
      console.error(`[P0] JS Error on ${page.url()}:`, err.message);
      p0_count++;
    });
    page.on('console', msg => {
      if (msg.type() === 'error') {
        // Some network errors or console errors
        console.error(`[Console Error] on ${page.url()}:`, msg.text());
      }
    });
  });

  test('Admin Role Audit', async ({ page }) => {
    await loginAs(page, 'admin');

    for (const route of ROUTES_TO_TEST) {
      testedRoutes.add(route.split('?')[0]);
      
      const startTime = Date.now();
      const response = await page.goto(route);
      const loadTime = Date.now() - startTime;
      
      if (!response || !response.ok()) {
        if (response && response.status() === 404) {
          console.error(`[P0] 404 Route: ${route}`);
          p0_count++;
        } else if (response && response.status() >= 500) {
          console.error(`[P0] 500 API Error: ${route}`);
          p0_count++;
        }
      }

      await captureStates(page, route, 'admin');

      // Responsive Assertions
      const hasHorizontalScroll = await page.evaluate(() => {
        return document.documentElement.scrollWidth > document.documentElement.clientWidth;
      });
      if (hasHorizontalScroll) {
        if (test.info().project.name.includes('Mobile') || test.info().project.name.includes('Tablet')) {
          console.error(`[P0] Horizontal Scroll detected on ${route}`);
          p0_count++;
        } else {
          console.error(`[P2] Horizontal Scroll detected on ${route}`);
          p2_count++;
        }
      }
    }
  });

  test('Test User Role Audit', async ({ page }) => {
    await loginAs(page, 'testuser');

    for (const route of ROUTES_TO_TEST) {
      testedRoutes.add(route.split('?')[0]);
      
      const response = await page.goto(route);
      
      if (route === '/mission-control') {
        // Ensure user cannot access admin page
        const text = await page.textContent('body');
        if (!text || (!text.includes('Forbidden') && !text.includes('403') && response && response.status() !== 403 && !page.url().includes('login'))) {
          console.error(`[P0] Unauthorized visibility for user on ${route}`);
          p0_count++;
        }
      } else {
        await captureStates(page, route, 'testuser');
      }
    }
  });

  test.afterAll(async () => {
    console.log(`\n--- UI Freeze Certification Results ---`);
    console.log(`P0 Critical Bugs: ${p0_count}`);
    console.log(`P1 High Bugs:     ${p1_count}`);
    console.log(`P2 Medium Bugs:   ${p2_count}`);
    
    // Route Coverage Check
    let untested = 0;
    for (const r of registeredRoutes) {
      if (!testedRoutes.has(r) && r !== '/auth/local-login' && r !== '/auth/logout' && !r.startsWith('/auth') && r !== '/pricing' && !r.startsWith('/uploads')) {
        console.warn(`[Coverage] Untested Route: ${r}`);
        untested++;
      }
    }
    
    console.log(`Untested Routes: ${untested}`);
    
    let isPass = true;
    if (p0_count > 0 || p1_count > 0 || p2_count > 5 || untested > 0) {
      isPass = false;
    }
    
    console.log(`\nUI FREEZE READY = ${isPass ? 'PASS' : 'FAIL'}`);
    
    fs.writeFileSync('test-results/certification_summary.json', JSON.stringify({
      p0: p0_count,
      p1: p1_count,
      p2: p2_count,
      untested_routes: untested,
      pass: isPass
    }, null, 2));
  });
});
