"""Retain grounded candidate ratings when turning ranges into review items."""
import math
from pathlib import Path
from core.utils import read_json


def rating_for_ranges(parts,candidates):
 details=[]
 for candidate in candidates:
  score=candidate.get('score')
  if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not 0<=score<=100:continue
  weight=sum(max(0,min(p['end'],candidate['end'])-max(p['start'],candidate['start'])) for p in parts)
  if weight>0:
   details.append({'title':candidate.get('title',''),'score':score,'overlap_seconds':weight,'reason':candidate.get('reason','')})
 if not details:return None,[]
 score=round(sum(d['score']*d['overlap_seconds'] for d in details)/sum(d['overlap_seconds'] for d in details),1)
 return score,details


def restore_saved_ratings(cfg):
 """Recover old lost scores from saved selection JSON; never invent a rating."""
 from storage.workspaces import WorkspaceStore
 from storage.plans import PlanStore
 restored=0;cache={}
 with WorkspaceStore(cfg['database']) as db,PlanStore(cfg['database']) as plans:
  for workspace in db.list_workspaces():
   changed=False
   for variant in workspace['variants']:
    for item in variant['items']:
     if item.get('score') not in (None,0):continue
     pid=item.get('source',{}).get('project_id')
     if not pid:continue
     if pid not in cache:
      cache[pid]=[]
      for plan in plans.list_plans(pid):
       try:
        index=read_json(Path(plan['index_path']));cache[pid].extend(index.get('clips',[]))
       except (OSError,KeyError,ValueError):pass
     score,details=rating_for_ranges(item['segments'],cache[pid])
     if score is not None:
      item.update(score=score,score_details=details,score_source='saved_selection');changed=True;restored+=1
   if changed:db.save_workspace(workspace)
 return restored
