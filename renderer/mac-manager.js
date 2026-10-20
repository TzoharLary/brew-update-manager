// Automatic availability results; review and approval remain separate actions.
function macAlternativeLabel(status) {
  status=status||'checking_pending';
  const he=i18n.lang==='he';
  const labels={
    binary_available:['נמצאה חבילה מוכנה ב־MacPorts','Ready package available in MacPorts'],
    mapping_unverified:['אין התאמה מאומתת ב־MacPorts','No verified MacPorts mapping'],
    catalog_unverified:['בדיקת החלופה ממתינה לרענון מידע','Alternative check awaits catalog refresh'],
    checking_pending:['החלופה תיבדק אוטומטית בבדיקה הבאה','Alternative will be checked automatically on the next scan'],
    older_than_installed:['החלופה ישנה מהגרסה המותקנת — ירידה בגרסה חסומה','Alternative is older than the installed version; downgrade blocked'],
    not_newer:['אין גרסה חדשה יותר במסלול זה','No newer upstream version on this route'],
    version_unverified:['לא ניתן לאמת התאמת גרסאות','Version compatibility could not be verified'],
    archive_missing:['חבילה מוכנה או תלות חסרה באתרים שנבדקו','Ready package or dependency is missing from checked sites'],
    archive_unknown:['לא ניתן לאמת זמינות בגלל שגיאת תקשורת','Availability could not be verified due to network errors'],
    resolution_failed:['לא ניתן להשלים בדיקת תלויות','Dependency resolution could not be completed'],
    architecture_unverified:['לא אומתה התאמה למחשב','Compatibility with this computer is unverified'],
    variants_unverified:['נדרשת בדיקת אפשרויות ההתקנה הקיימת','Installed variants need separate review'],
    excluded:['החבילה או תלות שלה מוחרגת','Package or dependency is excluded'],
    check_failed:['בדיקת החלופה נכשלה','Alternative check failed'],
  };
  return (labels[status]||[status,status])[he?0:1];
}
function renderMacManager() {
  let panel=document.getElementById('macManagerPanel');
  if (!panel) {
    panel=document.createElement('details');panel.id='macManagerPanel';panel.className='panel';
    panel.style.padding='16px';panel.style.marginBlock='16px';document.querySelector('.tabs').before(panel);
  }
  panel.replaceChildren();const he=i18n.lang==='he';
  const title=document.createElement('summary');
  title.textContent=(he?'עדכונים חסומים וחלופות':'Blocked updates and alternatives')+` (${snapshot?.migrations?.length||0})`;
  panel.appendChild(title);
  const hint=document.createElement('p');
  hint.textContent=he?'עדכון חבילה יוצג רק עם נתיב בינארי מאומת. פעולות מעבר שאינן מוכנות חסומות.':'An update is executable only with a verified binary route. Unfinished migration paths remain blocked.';
  for (const decision of snapshot?.update_decisions||[]) {
    if (decision.status==='current') continue;
    const line=document.createElement('p');
    const route=decision.current_provider===decision.target_provider?decision.current_provider:`${decision.current_provider} → ${decision.target_provider}`;
    line.textContent=`${decision.logical_id}: ${decision.installed_version||'—'} → ${decision.target_version||'—'} · ${route} · ${decision.status} · ${(decision.reason_codes||[]).join(', ')}`;
    panel.appendChild(line);
  }
  panel.appendChild(hint);
  const labels={fresh:he?'עדכני':'current',refreshed:he?'רוענן אוטומטית':'refreshed automatically',stale:he?'ירוענן בבדיקה הבאה':'will refresh on next check',awaiting_authorization:he?'הרענון ממתין להזדהות; לחץ התחל בדיקה':'refresh awaits authorization; press Start Check',refresh_failed:he?'הרענון נכשל או לא אושר; התחל בדיקה ינסה שוב':'refresh failed or was declined; Start Check will retry',not_installed:he?'MacPorts אינו מותקן':'MacPorts is not installed'};
  for(const [name,provider] of Object.entries(snapshot?.providers||{})) {
    const status=document.createElement('p');
    status.textContent=`${name}: ${provider.ok?'✓':'⚠'} · ${provider.checked_at||'—'}`;
    if(provider.catalog)status.textContent+=' · '+(labels[provider.catalog.status]||provider.catalog.status);
    if(provider.error)status.title=provider.error;
    panel.appendChild(status);
  }
  if(['running','needs_attention'].includes(snapshot?.migration_journal?.status)) {
    const warning=document.createElement('p');warning.textContent=he?'מעבר קודם דורש בדיקה לפני המשך.':'A previous migration needs review before continuing.';panel.appendChild(warning);
  }
  for(const offer of snapshot?.migrations||[]) {
    const row=document.createElement('details');row.style.marginBlock='10px';
    const summary=document.createElement('summary');
    summary.textContent=`${offer.name} — ${macAlternativeLabel(offer.status)}`;
    if(offer.target&&offer.installed_version)summary.textContent+=` · ${offer.installed_version} → ${offer.target}`;
    row.appendChild(summary);
    const detail=document.createElement('p');
    detail.textContent=(offer.ports?.length?`${offer.ports.join(', ')} · `:'')+(offer.detail||'');
    if(offer.checked_at)detail.textContent+=' · '+(he?'נבדק: ':'Checked: ')+offer.checked_at;
    row.appendChild(detail);
    const classification = offer.classification || {category:'unclassified'};
    const installation=(snapshot?.packages||[]).find(p=>p.provider==='homebrew'&&p.kind==='formula'&&p.name===offer.name)?.inventory_verification;
    if(installation) {
      const evidence=document.createElement('details');const title=document.createElement('summary');
      title.textContent=he?'ראיות ההתקנה':'Installation evidence';
      const text=document.createElement('p');text.style.whiteSpace='pre-wrap';
      text.textContent=(he?'גרסה לפי ההתקנה הפעילה: ':'Active installation version: ')+installation.package_version+'\n'+
        (he?'גרסאות שדווחו: ':'Manager versions: ')+(installation.manager_versions||[]).join(', ')+'\n'+
        (installation.active_keg||'')+'\n'+(installation.command_resolution?.resolved_path||'')+'\n'+
        [...(installation.issues||[]),...(installation.warnings||[])].join(' · ');
      if(installation.version_probe?.code)text.textContent+='\n'+installation.version_probe.output;
      evidence.append(title,text);row.appendChild(evidence);
    }
    const classificationLabels = {
      service:['שירות עם הגדרות ונתונים','Service with configuration and data'],
      shared_environment:['ספרייה או סביבת פיתוח משותפת','Shared library or development environment'],
      standalone_candidate:['כלי עצמאי מועמד לאוטומציה','Standalone automation candidate'],
      insufficient_information:['חסר מידע לסיווג','Insufficient classification evidence'],
      unclassified:['טרם סווג','Not yet classified'],
    };
    const classificationText=document.createElement('p');
    classificationText.className='migration-classification';
    classificationText.textContent=(he?'סיווג: ':'Classification: ')+(classificationLabels[classification.category]||classificationLabels.unclassified)[he?0:1];
    row.appendChild(classificationText);
    const reasons=document.createElement('p');
    reasons.textContent=(classification.reasons||[]).map(migrationReasonLabel).join(' · ');
    if(classification.checked_at) reasons.textContent+=' · '+classification.checked_at;
    row.appendChild(reasons);
    if(classification.consumers?.length) {
      const consumers=document.createElement('p');consumers.textContent=(he?'תלויים מתועדים: ':'Documented consumers: ')+classification.consumers.map(c=>c.name).join(', ');row.appendChild(consumers);
    }
    for(const [label,values] of [[he?'מידע חסר':'Missing evidence',classification.gaps],[he?'קובצי ראיות':'Evidence files',classification.evidence_refs]]) {
      if(values?.length) {const evidence=document.createElement('details');const title=document.createElement('summary');title.textContent=label;const text=document.createElement('p');text.textContent=values.join(' · ');evidence.append(title,text);row.appendChild(evidence);}
    }
    if(offer.status==='binary_available') {
      if(offer.below_homebrew_target) {
        const version=document.createElement('p');
        version.textContent=he?`זו גרסה חדשה מהמותקנת אצלך. יעד Homebrew הוא ${offer.homebrew_target}; אין בכך הוכחה שזו הגרסה האחרונה הנתמכת ב־Intel.`:`This is newer than your installed version. Homebrew targets ${offer.homebrew_target}; this does not prove it is the last Intel-supported release.`;
        row.appendChild(version);
      }
      const availability=document.createElement('p');
      availability.textContent=he?'נבדקה זמינות קבצים לכל התלויות. חתימות ותפקוד יאומתו רק בהתקנה.':'Archive availability was checked for all dependencies. Signatures and functionality are verified during installation.';row.appendChild(availability);
      if(!offer.executable) {
        const blocked=document.createElement('p');blocked.textContent=classification.category==='standalone_candidate'?(he?'מנגנון המעבר לכלי זה טרם מומש.':'The migration mechanism for this tool is not implemented yet.'):(he?'המעבר דורש טיפול בצרכנים, בנתונים או במידע החסר; אינו מבוצע בעדכון רגיל.':'Migration needs consumer, data or missing-evidence review; regular updates do not perform it.');row.appendChild(blocked);
      } else {
        const review=document.createElement('button');review.textContent=he?'סקירת תוכנית המעבר':'Review migration plan';
        review.disabled=!!busyLoading||!!lastKnownOperationRunning;
        review.onclick=async()=>{
          review.disabled=true;setLoading(true,'message.loadingState');startProgressPolling();
          try {
            const {plan}=await window.brewApp.migrationPlan(offer.name);
            const contents=document.createElement('p');
            contents.textContent=`${plan.installed_version} → ${plan.target} · ${plan.graph.nodes.map(n=>n.name).join(', ')} · ${plan.paths.join(', ')} · ${he?'צרכנים':'Consumers'}: ${plan.consumers.join(', ')||'—'}`;row.appendChild(contents);
            const approve=document.createElement('button');approve.textContent=he?'אישור מעבר חד־פעמי':'Approve this migration once';
            approve.onclick=async()=>{
              if(!confirm(he?'לאשר התקנה, בדיקת שימוש והסרת העותק הקודם רק אם אין לו תלויים?':'Approve installation, verification, and removal of the old copy only if unused?'))return;
              approve.disabled=true;setLoading(true,'message.loadingState');startProgressPolling();
              try {await window.brewApp.migrationApprove(plan.id);const response=await window.brewApp.getState();snapshot=response.snapshot||response;renderCards(snapshot);renderTable();}
              catch(error){showMessage(error.message,true);}
              finally{stopProgressPolling();setLoading(false,'message.loadingState');approve.disabled=false;}
            };row.appendChild(approve);
          }catch(error){showMessage(error.message,true);}
          finally{stopProgressPolling();setLoading(false,'message.loadingState');review.disabled=false;}
        };row.appendChild(review);
      }
    }
    panel.appendChild(row);
  }
}
