/* Alma Kit — helpers compartidos de los módulos Alma.
   El template define window.ALMA_TOKEN y window.ALMA_API antes de cargar este script. */
(function(){
  const TOKEN = window.ALMA_TOKEN || "";
  const API   = window.ALMA_API   || "";
  const H = { "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json" };

  const clp = n => "$" + (Math.round(n||0)).toLocaleString("es-CL");
  const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

  function toast(msg, err){
    let t = document.getElementById('alma-toast');
    if(!t){ t=document.createElement('div'); t.id='alma-toast'; t.className='toast'; document.body.appendChild(t); }
    t.textContent=msg; t.className='toast show'+(err?' err':'');
    setTimeout(()=>t.className='toast', 2300);
  }

  async function api(path, opts){
    const r = await fetch(API+path, { headers:H, ...(opts||{}) });
    if(!r.ok){
      let d={}; try{ d=await r.json(); }catch(e){}
      throw new Error(d.detail || ("HTTP "+r.status));
    }
    const ct = r.headers.get("content-type")||"";
    return ct.includes("json") ? r.json() : r.text();
  }

  function openModal(id){ document.getElementById(id).classList.add('show'); }
  function closeModal(id){ document.getElementById(id).classList.remove('show'); }

  // cerrar modales con Escape / click en backdrop
  document.addEventListener('keydown', e=>{ if(e.key==='Escape') document.querySelectorAll('.modal-bg.show').forEach(m=>m.classList.remove('show')); });
  document.addEventListener('click', e=>{ if(e.target.classList && e.target.classList.contains('modal-bg')) e.target.classList.remove('show'); });

  // debounce util
  function debounce(fn, ms){ let t; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a), ms||250); }; }

  // formato fecha corta es-CL desde 'YYYY-MM-DD' o ISO
  function fdate(s){ if(!s) return ''; const d=String(s).slice(0,10).split('-'); return d.length===3?`${d[2]}-${d[1]}-${d[0]}`:s; }

  window.AlmaKit = { TOKEN, API, clp, esc, toast, api, openModal, closeModal, debounce, fdate };
})();
