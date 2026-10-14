/* Run against an isolated local test server: NODE_PATH=<playwright packages> node tests/browser_upload.cjs */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');
const vm=require('node:vm');

async function main() {
  vm.runInThisContext(fs.readFileSync('app/static/js/upload-hash.js','utf8'));
  for(const length of [0,1,55,56,63,64,65,127,128,129,1024,1024*1024+19]) {
    const bytes=crypto.randomBytes(length), hash=new FTUploadHash();
    for(let pos=0;pos<bytes.length;pos+=73) hash.update(bytes.subarray(pos,pos+73));
    assert.equal(hash.hex(),crypto.createHash('sha256').update(bytes).digest('hex'));
  }
  console.log('SHA-256 reference vectors and incremental boundaries passed');
  const browser=await chromium.launch({channel:process.env.FT_BROWSER_CHANNEL||'msedge',headless:true});
  const context=await browser.newContext();
  const page=await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const base=process.env.FT_TEST_URL||'http://127.0.0.1:8765';
  try {
    await page.goto(base+'/login');
    const csrf=(await context.cookies()).find(c=>c.name==='ft_csrf').value;
    const headers={'X-CSRF-Token':csrf};
    let response=await context.request.post(base+'/api/auth/login',{headers,data:{login:'admin',password:'admin'}});
    if(response.status()!==200) response=await context.request.post(base+'/api/auth/login',{headers,data:{login:'admin',password:'browser-test-123'}});
    assert.equal(response.status(),200);
    await context.request.post(base+'/api/auth/change-password',{headers,data:{current:'admin',password:'browser-test-123'}});
    await page.goto(base+'/dashboard');
    await page.waitForFunction(()=>window.FTUploads);
    const content=crypto.randomBytes(9*1024*1024+123);
    const name='browser-resume-'+Date.now()+'.bin';
    const offsets=[];let lostResponse=false, failSecond=true, reloadMode=false, secondCommit=false;
    await page.route('**/api/uploads/*',async route=>{
      const req=route.request();
      if(req.method()!=='PATCH') return route.continue();
      const offset=Number(req.headers()['upload-offset']);offsets.push(offset);
      if(failSecond&&offset>0) return route.abort('internetdisconnected');
      if(reloadMode&&offset>0) {
        const response=await route.fetch();secondCommit=true;
        // Let the server acknowledge the second chunk, then lose the browser response.
        await new Promise(resolve=>setTimeout(resolve,1000));
        return route.abort('internetdisconnected').catch(()=>{});
      }
      if(!lostResponse&&offset===0) {
        const response=await route.fetch();assert.equal(response.status(),200);
        lostResponse=true;return route.abort('internetdisconnected');
      }
      return route.continue();
    });
    await page.locator('#file-input').setInputFiles({name,mimeType:'application/octet-stream',buffer:content});
    await page.waitForFunction(()=>document.querySelector('.upload-item.failed'),{},{timeout:90000});
    let pending=await (await context.request.get(base+'/api/uploads')).json();
    const task=pending.items.find(x=>x.name===name);
    assert.equal(task.offset,4*1024*1024);
    assert.equal(offsets.filter(x=>x===0).length,1,'lost first response must not resend committed bytes');
    failSecond=false;reloadMode=true;
    await page.locator('.upload-item.failed .upload-retry').click();
    for(let i=0;i<100&&!secondCommit;i++) await new Promise(r=>setTimeout(r,100));
    assert(secondCommit);
    await page.reload();reloadMode=false;
    await page.waitForSelector('[data-pending-name]');
    assert((await page.locator('[data-pending-name]').textContent()).includes(name));
    const beforeResume=offsets.length;
    await page.locator('#file-input').setInputFiles({name,mimeType:'application/octet-stream',buffer:content});
    await page.waitForFunction(()=>document.querySelector('.upload-item.done'),{},{timeout:60000});
    assert.equal(offsets[beforeResume],8*1024*1024,'reselect after reload starts at persisted offset');
    const status=await (await context.request.get(base+'/api/uploads/'+task.id)).json();
    const downloaded=await context.request.get(base+status.file.download_url);
    assert.deepEqual(await downloaded.body(),content);
    assert.equal(status.upload.sha256,crypto.createHash('sha256').update(content).digest('hex'));
    await page.waitForTimeout(1300);
    await page.screenshot({path:'dist/browser-upload-verified.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('Browser: lost response, interrupted chunk, automatic reconciliation, retry, reload/reselect and exact download passed');
  } catch(error) {
    console.error('Browser diagnostic',await page.evaluate(()=>({url:location.href,queue:document.getElementById('upload-queue')?.outerHTML,toasts:document.body.innerText.slice(-1600)})),errors);
    await page.screenshot({path:'dist/browser-upload-failure.png',fullPage:true});
    throw error;
  } finally {await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
