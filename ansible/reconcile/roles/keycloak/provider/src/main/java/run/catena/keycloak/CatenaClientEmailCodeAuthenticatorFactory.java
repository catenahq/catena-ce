package run.catena.keycloak;

import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

import io.phasetwo.keycloak.magic.MagicLink;

import org.jboss.logging.Logger;
import org.keycloak.Config;
import org.keycloak.authentication.Authenticator;
import org.keycloak.authentication.AuthenticatorFactory;
import org.keycloak.models.AuthenticationExecutionModel.Requirement;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.models.utils.KeycloakModelUtils;
import org.keycloak.provider.ProviderConfigProperty;

/**
 * Registers the clients' emailed sign-in code. It offers no configuration and
 * needs no user before it runs. Its reference category is the one-time-code
 * one, which makes a code that does not sign in count toward the realm's
 * lockout. The factory owns the one-thread queue that mails the codes, so the
 * sign-in request never waits for a mail server.
 */
public class CatenaClientEmailCodeAuthenticatorFactory implements AuthenticatorFactory {

    public static final String PROVIDER_ID = "catena-client-email-code";
    static final String REFERENCE_CATEGORY = "otp";
    static final int QUEUE_SIZE = 100;

    private static final Logger LOG = Logger.getLogger(CatenaClientEmailCodeAuthenticatorFactory.class);
    private static final Requirement[] REQUIREMENT_CHOICES = {Requirement.ALTERNATIVE, Requirement.DISABLED};

    private KeycloakSessionFactory sessionFactory;
    private ThreadPoolExecutor executor;

    @Override
    public String getId() {
        return PROVIDER_ID;
    }

    @Override
    public Authenticator create(KeycloakSession session) {
        return new CatenaClientEmailCodeAuthenticator(this::submit, System::currentTimeMillis);
    }

    @Override
    public String getDisplayType() {
        return "Catena client email code";
    }

    @Override
    public String getReferenceCategory() {
        return REFERENCE_CATEGORY;
    }

    @Override
    public boolean isConfigurable() {
        return false;
    }

    @Override
    public Requirement[] getRequirementChoices() {
        return REQUIREMENT_CHOICES;
    }

    @Override
    public boolean isUserSetupAllowed() {
        return false;
    }

    @Override
    public String getHelpText() {
        return "Signs a client in with a code emailed to them, in place of the password.";
    }

    @Override
    public List<ProviderConfigProperty> getConfigProperties() {
        return List.of();
    }

    @Override
    public void init(Config.Scope config) {
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        sessionFactory = factory;
        executor = new ThreadPoolExecutor(1, 1, 0L, TimeUnit.SECONDS, new ArrayBlockingQueue<>(QUEUE_SIZE),
                new ThreadPoolExecutor.DiscardPolicy());
    }

    /** Queues the mailing of a code; a full queue drops it. */
    void submit(String realmId, String clientId, String address, String code) {
        if (executor == null) {
            return;
        }
        executor.execute(() -> {
            try {
                KeycloakModelUtils.runJobInTransaction(sessionFactory, session ->
                        CatenaClientEmailCodeAuthenticator.deliver(session, realmId, clientId, address, code,
                                (s, user, c) -> MagicLink.sendOtpEmail(s, user, c, false)));
            } catch (Throwable t) {
                LOG.error("Could not mail a sign-in code", t);
            }
        });
    }

    @Override
    public void close() {
        if (executor != null) {
            executor.shutdown();
        }
    }
}
