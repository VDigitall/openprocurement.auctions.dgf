# -*- coding: utf-8 -*-
from datetime import timedelta
from schematics.types import StringType, URLType
from schematics.types.compound import ModelType
from schematics.exceptions import ValidationError
from schematics.transforms import blacklist, whitelist
from schematics.types.serializable import serializable
from zope.interface import implementer
from openprocurement.api.models import (
    BooleanType, ListType, Feature, Period, PeriodEndRequired, get_now, TZ,
    validate_features_uniq, validate_lots_uniq,
)
from openprocurement.api.utils import calculate_business_date
from openprocurement.auctions.core.models import IAuction
from openprocurement.auctions.flash.models import (
    Auction as BaseAuction, Document as BaseDocument, Bid as BaseBid,
    Complaint as BaseComplaint, Cancellation as BaseCancellation,
    Contract as BaseContract, Award as BaseAward, Lot, edit_role,
    calc_auction_end_time, COMPLAINT_STAND_STILL_TIME,
)


class Document(BaseDocument):

    format = StringType(regex='^[-\w]+/[-\.\w\+]+$')
    documentType = StringType(choices=[
        'auctionNotice', 'awardNotice', 'contractNotice',
        'notice', 'biddingDocuments', 'technicalSpecifications',
        'evaluationCriteria', 'clarifications', 'shortlistedFirms',
        'riskProvisions', 'billOfQuantity', 'bidders', 'conflictOfInterest',
        'debarments', 'evaluationReports', 'winningBid', 'complaints',
        'contractSigned', 'contractArrangements', 'contractSchedule',
        'contractAnnexe', 'contractGuarantees', 'subContract',
        'eligibilityCriteria', 'contractProforma', 'commercialProposal',
        'qualificationDocuments', 'eligibilityDocuments', 'tenderNotice',
        'illustration', 'financialLicense', 'virtualDataRoom',
    ])

    def validate_hash(self, data, hash_):
        if data.get('documentType') == 'virtualDataRoom' and hash_:
            raise ValidationError(u'This field is not required.')

    def validate_format(self, data, format_):
        if data.get('documentType') != 'virtualDataRoom' and not format_:
            raise ValidationError(u'This field is required.')
        if data.get('documentType') == 'virtualDataRoom' and format_:
            raise ValidationError(u'This field is not required.')

    def validate_url(self, data, url):
        if data.get('documentType') == 'virtualDataRoom':
            URLType().validate(url)


class Bid(BaseBid):
    class Options:
        roles = {
            'create': whitelist('value', 'tenderers', 'parameters', 'lotValues', 'status', 'selfQualified'),
        }

    documents = ListType(ModelType(Document), default=list())
    selfQualified = BooleanType(required=True, choices=[True])


class Complaint(BaseComplaint):

    documents = ListType(ModelType(Document), default=list())


class Cancellation(BaseCancellation):

    documents = ListType(ModelType(Document), default=list())


class Contract(BaseContract):

    documents = ListType(ModelType(Document), default=list())


class Award(BaseAward):
    complaints = ListType(ModelType(Complaint), default=list())
    documents = ListType(ModelType(Document), default=list())


def validate_not_available(items, *args):
    if items:
        raise ValidationError(u"Option not available in this procurementMethodType")


@implementer(IAuction)
class Auction(BaseAuction):
    """Data regarding auction process - publicly inviting prospective contractors to submit bids for evaluation and selecting a winner or winners."""
    class Options:
        roles = {
            'edit_active.tendering': (blacklist('enquiryPeriod', 'tenderPeriod', 'value', 'auction_value', 'minimalStep', 'guarantee', 'auction_guarantee') + edit_role),
        }

    awards = ListType(ModelType(Award), default=list())
    bids = ListType(ModelType(Bid), default=list())  # A list of all the companies who entered submissions for the auction.
    cancellations = ListType(ModelType(Cancellation), default=list())
    complaints = ListType(ModelType(Complaint), default=list())
    contracts = ListType(ModelType(Contract), default=list())
    documents = ListType(ModelType(Document), default=list())  # All documents and attachments related to the auction.
    enquiryPeriod = ModelType(Period)  # The period during which enquiries may be made and will be answered.
    tenderPeriod = ModelType(Period)  # The period when the auction is open for submissions. The end date is the closing date for auction submissions.
    procurementMethodType = StringType(default="dgfOtherAssets")
    status = StringType(choices=['draft', 'active.tendering', 'active.auction', 'active.qualification', 'active.awarded', 'complete', 'cancelled', 'unsuccessful'], default='active.tendering')
    features = ListType(ModelType(Feature), validators=[validate_features_uniq, validate_not_available])
    lots = ListType(ModelType(Lot), default=list(), validators=[validate_lots_uniq, validate_not_available])

    @serializable(serialized_name="tenderPeriod", type=ModelType(Period))
    def auction_tenderPeriod(self):
        if self.tenderPeriod and self.tenderPeriod.endDate:
            return self.tenderPeriod
        endDate = calculate_business_date(self.auctionPeriod.startDate, -timedelta(days=1), self)
        return Period(dict(endDate=endDate))

    def initialize(self):
        if not self.enquiryPeriod:
            self.enquiryPeriod = type(self).enquiryPeriod.model_class()
        now = get_now()
        self.tenderPeriod.startDate = self.enquiryPeriod.startDate = now
        self.enquiryPeriod.endDate = self.tenderPeriod.endDate
        self.date = now
        if self.lots:
            for lot in self.lots:
                lot.date = now

    def validate_tenderPeriod(self, data, period):
        if not (period and period.endDate) and not ('auctionPeriod' in data and data['auctionPeriod'].startDate):
            raise ValidationError(u'This field is required.')

    def validate_value(self, data, value):
        if value.currency != u'UAH':
            raise ValidationError(u"currency should be only UAH")

    @serializable(serialize_when_none=False)
    def next_check(self):
        now = get_now()
        checks = []
        if self.status == 'active.tendering' and self.tenderPeriod and self.tenderPeriod.endDate:
            checks.append(self.tenderPeriod.endDate.astimezone(TZ))
        elif not self.lots and self.status == 'active.auction' and self.auctionPeriod and self.auctionPeriod.startDate and not self.auctionPeriod.endDate:
            if now < self.auctionPeriod.startDate:
                checks.append(self.auctionPeriod.startDate.astimezone(TZ))
            elif now < calc_auction_end_time(self.numberOfBids, self.auctionPeriod.startDate).astimezone(TZ):
                checks.append(calc_auction_end_time(self.numberOfBids, self.auctionPeriod.startDate).astimezone(TZ))
        elif self.lots and self.status == 'active.auction':
            for lot in self.lots:
                if lot.status != 'active' or not lot.auctionPeriod or not lot.auctionPeriod.startDate or lot.auctionPeriod.endDate:
                    continue
                if now < lot.auctionPeriod.startDate:
                    checks.append(lot.auctionPeriod.startDate.astimezone(TZ))
                elif now < calc_auction_end_time(lot.numberOfBids, lot.auctionPeriod.startDate).astimezone(TZ):
                    checks.append(calc_auction_end_time(lot.numberOfBids, lot.auctionPeriod.startDate).astimezone(TZ))
        elif not self.lots and self.status == 'active.awarded' and not any([
                i.status in self.block_complaint_status
                for i in self.complaints
            ]) and not any([
                i.status in self.block_complaint_status
                for a in self.awards
                for i in a.complaints
            ]):
            standStillEnds = [
                a.complaintPeriod.endDate.astimezone(TZ)
                for a in self.awards
                if a.complaintPeriod.endDate
            ]

            last_award_status = self.awards[-1].status if self.awards else ''
            if standStillEnds and last_award_status == 'unsuccessful':
                checks.append(max(standStillEnds))
        elif self.lots and self.status in ['active.qualification', 'active.awarded'] and not any([
                i.status in self.block_complaint_status and i.relatedLot is None
                for i in self.complaints
            ]):
            for lot in self.lots:
                if lot['status'] != 'active':
                    continue
                lot_awards = [i for i in self.awards if i.lotID == lot.id]
                pending_complaints = any([
                    i['status'] in self.block_complaint_status and i.relatedLot == lot.id
                    for i in self.complaints
                ])
                pending_awards_complaints = any([
                    i.status in self.block_complaint_status
                    for a in lot_awards
                    for i in a.complaints
                ])
                standStillEnds = [
                    a.complaintPeriod.endDate.astimezone(TZ)
                    for a in lot_awards
                    if a.complaintPeriod.endDate
                ]
                last_award_status = lot_awards[-1].status if lot_awards else ''
                if not pending_complaints and not pending_awards_complaints and standStillEnds and last_award_status == 'unsuccessful':
                    checks.append(max(standStillEnds))
        if self.status.startswith('active'):
            from openprocurement.api.utils import calculate_business_date
            for complaint in self.complaints:
                if complaint.status == 'claim' and complaint.dateSubmitted:
                    checks.append(calculate_business_date(complaint.dateSubmitted, COMPLAINT_STAND_STILL_TIME, self))
                elif complaint.status == 'answered' and complaint.dateAnswered:
                    checks.append(calculate_business_date(complaint.dateAnswered, COMPLAINT_STAND_STILL_TIME, self))
            for award in self.awards:
                for complaint in award.complaints:
                    if complaint.status == 'claim' and complaint.dateSubmitted:
                        checks.append(calculate_business_date(complaint.dateSubmitted, COMPLAINT_STAND_STILL_TIME, self))
                    elif complaint.status == 'answered' and complaint.dateAnswered:
                        checks.append(calculate_business_date(complaint.dateAnswered, COMPLAINT_STAND_STILL_TIME, self))
        return min(checks).isoformat() if checks else None