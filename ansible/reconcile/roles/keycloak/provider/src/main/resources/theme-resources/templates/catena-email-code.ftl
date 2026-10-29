<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=true displayInfo=false; section>
    <#if section = "header">
        <#if catenaEmailCodeState = "code">${msg("catena-email-code-code-title")}<#else>${msg("catena-email-code-address-title")}</#if>
    <#elseif section = "form">
        <form id="kc-catena-email-code-form" class="${properties.kcFormClass!}" action="${url.loginAction}" method="post">
            <#if catenaEmailCodeState = "code">
                <p>${msg("catena-email-code-sent")}</p>
                <div class="${properties.kcFormGroupClass!}">
                    <label for="code" class="${properties.kcLabelClass!}">${msg("catena-email-code-code-label")}</label>
                    <input id="code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" autofocus
                           class="${properties.kcInputClass!}" />
                </div>
                <div class="${properties.kcFormGroupClass!}">
                    <input class="${properties.kcButtonClass!} ${properties.kcButtonPrimaryClass!} ${properties.kcButtonBlockClass!} ${properties.kcButtonLargeClass!}"
                           type="submit" value="${msg("catena-email-code-sign-in")}" />
                </div>
                <div class="${properties.kcFormGroupClass!}">
                    <input class="${properties.kcButtonClass!} ${properties.kcButtonDefaultClass!} ${properties.kcButtonBlockClass!} ${properties.kcButtonLargeClass!}"
                           type="submit" name="resend" formnovalidate value="${msg("catena-email-code-resend")}" />
                </div>
            <#else>
                <div class="${properties.kcFormGroupClass!}">
                    <label for="address" class="${properties.kcLabelClass!}">${msg("catena-email-code-address-label")}</label>
                    <input id="address" name="address" type="email" autocomplete="email" autofocus
                           class="${properties.kcInputClass!}" />
                </div>
                <div class="${properties.kcFormGroupClass!}">
                    <input class="${properties.kcButtonClass!} ${properties.kcButtonPrimaryClass!} ${properties.kcButtonBlockClass!} ${properties.kcButtonLargeClass!}"
                           type="submit" value="${msg("catena-email-code-send")}" />
                </div>
            </#if>
        </form>
    </#if>
</@layout.registrationLayout>
