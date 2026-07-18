# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""Recurrent scheduling algorithm (id 3, name RECURRENT).

Targets are only scheduled if they were never observed or if they were
observed more than a specified number of days in the past; the selection
itself uses the :class:`Higher` algorithm.
"""

import datetime
import logging

from sqlalchemy import and_, or_

from chimera_robobs.scheduling.algorithms.base import RecurrentError
from chimera_robobs.scheduling.algorithms.higher import Higher
from chimera_robobs.scheduling.dates import datetime_from_mjd
from chimera_robobs.scheduling.model import ObsBlock, RecurrentDB

log = logging.getLogger(__name__)


class Recurrent(Higher):
    id = 3
    name = "RECURRENT"
    default_slot_len = 1800.0
    timed_constraint = True

    def process(
        self, *, obs_start, obs_end, query, config=None, slot_len=None, today=None
    ):
        # Try to read the recurrence time from the configuration. If none is
        # provided, raise an exception.
        if not config or "recurrence" not in config:
            raise RecurrentError(
                "No configuration file provided or no recurrence time defined."
            )

        recurrence_time = config["recurrence"]

        # Filter targets by observing date. Leave "NeverObserved" and those
        # observed more than recurrence_time days ago.  ``today`` is only
        # passed by simulations.
        if today is None:
            today = self.site.ut().replace(tzinfo=None)
        else:
            today = today.replace(tzinfo=None)
        reference_date = today - datetime.timedelta(days=recurrence_time)

        ntargets = query.count()
        # Exclude targets that were observed less than the specified amount
        # of time ago.
        query = query.filter(
            or_(
                ObsBlock.observed == False,  # noqa: E712
                and_(
                    ObsBlock.observed == True,  # noqa: E712
                    ObsBlock.last_observation < reference_date,
                ),
            )
        )
        log.debug("Filtering %i of %i targets", query.count(), ntargets)
        # Select targets with the Higher algorithm
        return super().process(
            obs_start=obs_start,
            obs_end=obs_end,
            query=query,
            config=config,
            slot_len=slot_len,
        )

    def add(self, block):
        session = self.session()
        try:
            obsblock = session.merge(block[0])

            # Check if this is already in the database
            recurrent_block = (
                session.query(RecurrentDB)
                .filter(
                    RecurrentDB.pid == obsblock.pid,
                    RecurrentDB.block_id == obsblock.id,
                    RecurrentDB.target_id == obsblock.target_id,
                )
                .first()
            )

            if recurrent_block is None:
                # Not in the database, add it
                recurrent_block = RecurrentDB(
                    pid=obsblock.pid,
                    block_id=obsblock.id,
                    target_id=obsblock.target_id,
                )
                session.add(recurrent_block)
        finally:
            session.commit()

    def observed(self, time, program, soft=False):
        """Process program as observed."""
        obstime = datetime_from_mjd(time).replace(tzinfo=None)

        session = self.session()
        try:
            prog = session.merge(program[0])
            prog.finished = True
            obsblock = session.merge(program[2])
            obsblock.observed = True

            log.debug("%s: Marking as observed @ %s", obsblock.pid, obstime)

            if not soft:
                log.debug("Running in hard mode. Storing main information in database.")
                obsblock.last_observation = obstime
                recurrent_block = (
                    session.query(RecurrentDB)
                    .filter(
                        RecurrentDB.pid == obsblock.pid,
                        RecurrentDB.block_id == obsblock.id,
                        RecurrentDB.target_id == obsblock.target_id,
                    )
                    .first()
                )
                if recurrent_block is None:
                    log.debug("Block not in recurrent database. Adding block...")
                    recurrent_block = RecurrentDB(
                        pid=obsblock.pid,
                        block_id=obsblock.id,
                        target_id=obsblock.target_id,
                        visits=1,
                        last_visit=obstime,
                    )
                    session.add(recurrent_block)
                else:
                    recurrent_block.visits += 1
                    recurrent_block.last_visit = obstime

                    if 0 < recurrent_block.max_visits < recurrent_block.visits:
                        log.debug(
                            "Max visits (%i) reached. Marking as complete.",
                            recurrent_block.max_visits,
                        )
                        obsblock.completed = True
                    else:
                        log.debug("%i visits completed.", recurrent_block.visits)
            else:
                log.debug("Running in soft mode...")
        finally:
            session.commit()
