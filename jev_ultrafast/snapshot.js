(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  // Password fields are offered as targets. Login field values never leave the page unmasked.
  const secret = e => e.tagName==='INPUT' && e.type==='password';
  const login = e => e.tagName==='INPUT' && ['email','password'].includes(e.type);
  const masked = e => login(e) ? (e.value ? '********' : '') : e.value;
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  // Icon-only controls often have no accessible name. Recover a hint from dismiss attributes,
  // icon ligature text (e.g. Material "close"), or class names, instead of offering a bare "button".
  const hint = e => {
    if (['mat-dialog-close','data-dismiss','data-bs-dismiss'].some(a=>e.hasAttribute(a))) return 'Close dialog';
    const icon=e.querySelector('mat-icon,.material-icons,.material-symbols-outlined')?.textContent.trim();
    if (icon) return icon.replace(/_/g,' ')+' (icon)';
    if (/(^|[\s_-])close([\s_-]|$)/i.test(e.getAttribute('class')||'')) return 'Close';
    const fragment=e.tagName==='A' && (e.getAttribute('href')||'').match(/^#(.+)/);
    if (fragment) return fragment[1].replace(/[-_]/g,' ')+' (in-page link)';
    return '';
  };
  // Short option labels ("7:00 PM") are ambiguous without their row heading ("Thursday, ...").
  // Take the first line of the nearest ancestor text that is not another control's own label.
  const context = e => {
    let a=e.parentElement;
    for (let i=0;i<6 && a && a!==document.body;i++,a=a.parentElement) {
      let text=a.innerText||'';
      for (const c of a.querySelectorAll('label,button,input,select,textarea,a'))
        if (c.innerText) text=text.replace(c.innerText,'');
      const line=text.split('\n').map(s=>s.trim()).find(Boolean);
      if (line) return line.slice(0,80);
    }
    return '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel','password'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),masked(e),e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),masked(e)??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  // Leaf click-listener targets (e.g. event cards) that contain no real control of their own.
  const clickable=new Set([...(window.__jevClickable||[])].filter(e=>e.isConnected &&
    e!==document.body && e!==document.documentElement && !e.matches(selector) &&
    !e.closest(selector) && !e.querySelector(selector)));
  const actions=[];
  for (const e of [...document.querySelectorAll(selector), ...clickable]) {
    if ((!safe(e) && !secret(e)) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
    const rname=role(e) || (clickable.has(e) ? 'button' : null);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||hint(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    if (['checkbox','radio','spinbutton'].includes(rname) || base.label===rname) {
      const where=context(e);
      if (where && !base.label.includes(where)) base.label=where+' · '+base.label;
    }
    if (e.tagName==='INPUT' && ['email','password'].includes(e.type)) base.input_type=e.type;
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(masked(e)) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  const words=[], below=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  let belowLength=0;
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    } else if (r.width>0 && r.height>0 && r.top>=innerHeight && belowLength<400) {
      // A short preview of what scrolling down would reveal, so the policy knows a form is below.
      below.push(value); belowLength+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const text_below=below.join('\n').slice(0,400);
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,text_below,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions};
})()
