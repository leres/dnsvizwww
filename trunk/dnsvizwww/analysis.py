import datetime
import time

import dns.rdatatype

from django.db import transaction

import dnsviz.analysis
import dnsviz.format as fmt
from models import DomainName, DomainNameAnalysis

MIN_ANALYSIS_INTERVAL = 14400
MAX_ANALYSIS_TIME = 300

class Analyst(dnsviz.analysis.Analyst):
    qname_only = False
    analysis_model = DomainNameAnalysis

    clone_attrnames = dnsviz.analysis.Analyst.clone_attrnames + ['force_ancestry','start_time']

    def __init__(self, name, dlv_domain=None, client_ipv4=None, client_ipv6=None, ceiling=None, force_dnskey=False,
             follow_ns=False, trace=None, explicit_delegations=None, analysis_cache=None, analysis_cache_lock=None, start_time=None, force_ancestry=False, force_self=True):

        super(Analyst, self).__init__(name, dlv_domain=dlv_domain, client_ipv4=client_ipv4, client_ipv6=client_ipv6, ceiling=ceiling,
                force_dnskey=force_dnskey, follow_ns=follow_ns, trace=trace, explicit_delegations=explicit_delegations, analysis_cache=analysis_cache, analysis_cache_lock=analysis_cache_lock)
        if start_time is None:
            start_time = datetime.datetime.now(fmt.utc).replace(microsecond=0)
        self.start_time = start_time
        self.force_ancestry = force_ancestry
        self.force_self = force_self

    def _analyze_dlv(self):
        if self.dlv_domain is not None and self.dlv_domain != self.name and self.dlv_domain not in self.analysis_cache:
            kwargs = dict([(n, getattr(self, n)) for n in self.clone_attrnames])
            kwargs['ceiling'] = self.dlv_domain
            a = self.__class__(self.dlv_domain, force_dnskey=False, force_self=False, **kwargs)
            a.analyze()

    def unsaved_dependencies(self, name_obj, trace=None):
        if trace is None:
            trace = []

        unsaved_names = []
        if name_obj.name in trace:
            return unsaved_names
        
        for cname, cname_obj in name_obj.cname_targets.items():
            if cname_obj is None or cname_obj.pk is None:
                unsaved_names.append(cname)
                if cname_obj is not None:
                    unsaved_names.extend(self.unsaved_dependencies(cname_obj, trace+[name_obj.name]))
        for dname, dname_obj in name_obj.dname_targets.items():
            if dname_obj is None or dname_obj.pk is None:
                unsaved_names.append(dname)
                if dname_obj is not None:
                    unsaved_names.extend(self.unsaved_dependencies(dname_obj, trace+[name_obj.name]))
        for signer, signer_obj in name_obj.external_signers.items():
            if signer_obj is None or signer_obj.pk is None:
                unsaved_names.append(signer)
                if signer_obj is not None:
                    unsaved_names.extend(self.unsaved_dependencies(signer_obj, trace+[name_obj.name]))
        if self.follow_ns:
            for target, ns_obj in name_obj.ns_dependencies.items():
                if ns_obj is None or ns_obj.pk is None:
                    unsaved_names.append(target)
                    if ns_obj is not None:
                        unsaved_names.extend(self.unsaved_dependencies(ns_obj, trace+[name_obj.name]))

        return unsaved_names

    def _analyze_stub(self, name):
        name_obj, created = super(Analyst, self)._analyze_stub(name)
        if created:
            self._save_analysis(name_obj)
        return name_obj, created

    def _analyze(self, name):
        name_obj, created = super(Analyst, self)._analyze(name)
        if created:
            self._save_analysis(name_obj)
        return name_obj, created

    def _save_analysis(self, name_obj):
        # if this object hasn't been saved already (it might have been
        # retrieved from the database) and it is either a zone or the name in
        # question, then save it.
        if name_obj.pk is not None or not (name_obj.is_zone() or name_obj.name == self.name):
            return

        if name_obj.dep_analysis_end is None:
            if name_obj.stub:
                name_obj.dep_analysis_end = name_obj.analysis_end
            else:
                name_obj.dep_analysis_end = datetime.datetime.now(fmt.utc).replace(microsecond=0)
            self.analysis_cache[name_obj.name] = name_obj

        # check for cyclic dependencies.  if there are no unsaved
        # dependencies in the trace (which will cause everything to be
        # saved in a single transaction) then go ahead and save.
        unsaved_deps = self.unsaved_dependencies(name_obj)
        names_in_trace = [n for n,r in self.trace]
        unsaved_dep_in_trace = False
        for dep in unsaved_deps:
            if dep in names_in_trace:
                unsaved_dep_in_trace = True
        if not unsaved_dep_in_trace:
            with transaction.commit_manually():
                try:
                    name_obj.save_all()
                except:
                    transaction.rollback()
                    raise
                else:
                    transaction.commit()
        self.analysis_cache[name_obj.name] = name_obj

    def _get_name_for_analysis(self, name, stub=False, lock=True):
        with self.analysis_cache_lock:
            try:
                name_obj = self.analysis_cache[name]
                wait_for_analysis = True
            except KeyError:
                if lock:
                    name_obj = self.analysis_cache[name] = self.analysis_model(name, stub=stub)
                wait_for_analysis = False

        # name is now locked locally (for threads that use analysis_cache) but
        # now we lock it across the database
        if not wait_for_analysis:
            while True:
                # retrieve the freshest DomainNameAnalysis from the DB
                fresh_name_obj = self.analysis_model.objects.latest(name)

                # if no analysis is necessary, then simply return
                if not self._analyze_or_not(fresh_name_obj):
                    rdtypes = set([dns.rdatatype.NS])
                    if fresh_name_obj.referral_rdtype is not None:
                        rdtypes.add(fresh_name_obj.referral_rdtype)
                    fresh_name_obj.retrieve_ancestry(dnssec_rdtypes=False, follow_dependencies=False)
                    fresh_name_obj.retrieve_related(rdtypes=rdtypes)
                    self.analysis_cache[name] = fresh_name_obj
                    return fresh_name_obj

                # if not locking, then return None
                if not lock:
                    return None

                # get the name (or create it)
                dname_obj = DomainName.objects.get_or_create(name=name)[0]
                now = datetime.datetime.now(fmt.utc).replace(microsecond=0)

                attempt_lock = True
                # determine if there is an analysis for this name in progress
                if dname_obj.analysis_start is not None:
                    # if this analysis has been updated, then clean up the lock
                    if fresh_name_obj is not None and fresh_name_obj.analysis_start >= dname_obj.analysis_start:
                        pass
                    # if this analysis has gone stale, then reset it
                    elif now - dname_obj.analysis_start > datetime.timedelta(seconds=MAX_ANALYSIS_TIME):
                        pass
                    else:
                        attempt_lock = False

                # if there is no analysis, then attempt to get the lock for the name.
                # if lock was obtained, then return the name_obj
                if attempt_lock and DomainName.objects.filter(pk=dname_obj.pk, analysis_start=dname_obj.analysis_start).update(analysis_start=now):
                    return name_obj

                time.sleep(3)

        else:
            while name_obj.analysis_end is None:
                time.sleep(1)
                name_obj = self.analysis_cache[name]
            #TODO re-do analyses if force_dnskey is True and dnskey hasn't been queried
            #TODO re-do anaysis if not stub requested but cache is stub?
        return name_obj

    def _analyze_or_not(self, name_obj):
        if name_obj is None:
            return True

        force_analysis = self.force_self and (self.force_ancestry or self.name == name_obj.name)
        updated_since_analysis_start = name_obj.analysis_end > self.start_time

        min_ttl = None
        for rdtype in (dns.rdatatype.NS, -dns.rdatatype.NS, dns.rdatatype.DS, dns.rdatatype.DNSKEY):
            if rdtype in name_obj.ttl_mapping:
                if min_ttl is None or name_obj.ttl_mapping[rdtype] < min_ttl:
                    min_ttl = name_obj.ttl_mapping[rdtype]
            else:
                #TODO handle negative TTL
                pass

        if min_ttl is None or min_ttl < MIN_ANALYSIS_INTERVAL:
            min_ttl = MIN_ANALYSIS_INTERVAL

        time_since_analysis = datetime.datetime.now(fmt.utc).replace(microsecond=0) - name_obj.analysis_end
        maximum_time_allowed = datetime.timedelta(seconds=max(min_ttl, MIN_ANALYSIS_INTERVAL))
        analysis_due = time_since_analysis > maximum_time_allowed

        if force_analysis and not updated_since_analysis_start:
            return True
        if analysis_due:
            return True
        return False