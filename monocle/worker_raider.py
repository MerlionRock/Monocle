import traceback
from math import ceil
from queue import PriorityQueue
from time import time, monotonic
from asyncio import CancelledError, Semaphore, sleep
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from pogeo import get_distance

from .db import Fort, FortSighting, session_scope
from .utils import randomize_point
from .worker import Worker, UNIT, sb_detector, HARDCORE_HYPERDRIVE
from .shared import LOOP, call_at, get_logger
from .accounts import Account
from . import db_proc, bounds, spawns, sanitized as conf

log = get_logger("workerraider")

class GymNotFoundError(Exception):
    """Raised when gym is not found at the usual spot"""
    pass

class NothingSeenAtGymSpotError(Exception):
    """Raised when nothing is seen at the gym spot"""
    pass

class WorkerRaider(Worker):
    workers = [] 
    gym_scans = 0
    skipped = 0
    visits = 0
    hash_burn = 0
    workers_needed = int(ceil(conf.RAIDER_PERCENT_OF_WORKERS * conf.GRID[0] * conf.GRID[1]))
    job_queue = PriorityQueue()
    coroutine_semaphore = Semaphore(workers_needed, loop=LOOP)

    def __init__(self, worker_no, overseer, captcha_queue, account_queue, worker_dict, account_dict, start_coords=None):
        super().__init__(worker_no, overseer, captcha_queue, account_queue, worker_dict, account_dict, start_coords=start_coords)
        self.scan_delayed = 0

    def get_start_coords(self):
        return bounds.center

    def required_extra_accounts(self):
        return super().required_extra_accounts() + self.workers_needed

    @classmethod
    def preload(self):
        log.info("Preloading forts")
        with session_scope() as session:
            forts = session.query(Fort) \
                .options(joinedload(Fort.sightings)) \
                .filter(Fort.lat.between(bounds.south, bounds.north),
                        Fort.lon.between(bounds.west, bounds.east))
            try:
                for fort in forts:
                    if (fort.lat, fort.lon) not in bounds:
                        continue
                    obj = {
                        'id': fort.id,
                        'external_id': fort.external_id,
                        'lat': fort.lat,
                        'lon': fort.lon,
                        'name': fort.name,
                        'url': fort.url,
                        'last_modified': 0,
                        'updated': 0,
                    }
                    if len(fort.sightings) > 0:
                        sighting = fort.sightings[0]
                        obj['last_modified'] = sighting.last_modified
                        obj['updated'] = sighting.updated
                    self.add_job(obj)
            except Exception as e:
                log.error("ERROR: {}", e)
            log.info("Loaded {} forts", self.job_queue.qsize())
    
    @classmethod
    def add_job(self, gym):
        self.job_queue.put_nowait((gym.get('updated', gym.get('last_modified', 0)), time(), gym))

    @classmethod
    async def launch(self, overseer):
        self.overseer = overseer
        self.preload()
        try:
            await sleep(5)
            log.info("Couroutine launched.")
        
            # Initialize workers
            for x in range(self.workers_needed):
                try:
                    self.workers.append(WorkerRaider(worker_no=x,
                        overseer=overseer,
                        captcha_queue=self.overseer.captcha_queue,
                        account_queue=self.overseer.extra_queue,
                        worker_dict=self.overseer.worker_dict,
                        account_dict=self.overseer.account_dict))
                except Exception as e:
                    log.error("WorkerRaider initialization error: {}", e)
                    traceback.print_exc()
            log.info("WorkerRaider count: ({}/{})", len(self.workers), self.workers_needed)

            while True:
                try:
                    while not self.job_queue.empty():
                        job = self.job_queue.get()[2]
                        log.debug("Job: {}", job)

                        await self.coroutine_semaphore.acquire()
                        LOOP.create_task(self.try_point(job))
                except Exception as e:
                    log.warning("A wild error appeared in launcher loop: {}", e)
                await sleep(1)
        except CancelledError:
            log.info("Coroutine cancelled.")
        except Exception as e:
            log.warning("A wild error appeared in launcher: {}", e)

    @classmethod
    async def try_point(self, job):
        try:
            point = (job['lat'], job['lon'])
            fort_external_id = job['external_id']
            updated = job.get('updated', job.get('last_modified', 0))
            point = randomize_point(point,amount=0.00003) # jitter around 3 meters
            skip_time = monotonic() + (conf.SEARCH_SLEEP * 2)
            worker = await self.best_worker(point, job, updated, skip_time)
            if not worker:
                return
            async with worker.busy:
                #if worker.last_gmo:
                #    await worker.sleep_travel_time(point)
                visit_result = await worker.visit(point,
                        gym=job)
                if visit_result == -1:
                    self.hash_burn += 1
                    point = randomize_point(point,amount=0.00001) # jitter around 3 meters
                    #await worker.sleep_travel_time(point)
                    visit_result = await worker.visit(point,
                            gym=job)
                if visit_result:
                    if visit_result == -1:
                        raise GymNotFoundError("Gym disappeared")
                    else:
                        now = int(time())
                        worker.scan_delayed = now - updated
                        job['updated'] = now
                        self.visits += 1
                else:
                    raise NothingSeenAtGymSpotError("Nothing seen")
        except CancelledError:
            raise
        except (GymNotFoundError,NothingSeenAtGymSpotError) as e:
            self.skipped += 1
            log.error('Gym visit error: {}', e)
        except Exception as e:
            self.skipped += 1
            log.exception('An exception occurred in try_point: {}', e)
        finally:
            self.add_job(job)
            self.coroutine_semaphore.release()

    @classmethod
    async def best_worker(self, point, job, updated, skip_time):
        while self.overseer.running:
            gen = (w for w in self.workers if not w.busy.locked())
            try:
                worker = next(gen)
                lowest_speed = worker.travel_speed(point)
            except StopIteration:
                lowest_speed = float('inf')
            for w in gen:
                speed = w.travel_speed(point)
                if speed < lowest_speed:
                    lowest_speed = speed
                    worker = w
            tolerable_time_diff = 300
            time_diff = int(time() - updated)
            min_time_diff = max(min(time_diff, tolerable_time_diff * 5), 0)
            speed_limit = (conf.SPEED_LIMIT * (1.0 + (min_time_diff / tolerable_time_diff)))
            log.info("SPEED_LIMIT {}, time_diff: {}, speed_limit: {:.2f}, my_speed: {:.2f}", job.get('external_id'), time_diff, speed_limit, lowest_speed)
            if worker:# and lowest_speed < speed_limit:
                worker.speed = lowest_speed
                return worker
            if skip_time and monotonic() > skip_time:
                return None
            await sleep(conf.SEARCH_SLEEP, loop=LOOP)